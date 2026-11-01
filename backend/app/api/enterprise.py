"""Organization, lifecycle and operational endpoints sharing the same access policy."""
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import delete, func, select, update

from app.api.dependencies import DB, CurrentUser
from app.models import (
    AuditEvent,
    AuthSession,
    Document,
    DocumentRevision,
    IngestionJob,
    KnowledgeBase,
    KnowledgeBaseGrant,
    Membership,
    Organization,
    User,
)
from app.schemas import DocumentMetadataRequest, MemberRequest, OrganizationRequest, SourceSyncRequest
from app.services.access import audit, require_kb, require_org

router = APIRouter(prefix="/api")


@router.get("/organizations")
def organizations(db: DB, user: CurrentUser):
    return [{"id": org.id, "name": org.name, "role": member.role} for org, member in db.execute(
        select(Organization, Membership).join(Membership).where(Membership.user_id == user.id))]


@router.post("/organizations", status_code=201)
def create_organization(body: OrganizationRequest, db: DB, user: CurrentUser):
    org = Organization(name=body.name)
    db.add(org)
    db.flush()
    db.add(Membership(organization_id=org.id, user_id=user.id, role="admin"))
    audit(db, user.id, "organization.create", org.id, org.id)
    db.commit()
    return {"id": org.id, "name": org.name, "role": "admin"}


@router.get("/organizations/{org_id}/members")
def members(org_id: str, db: DB, user: CurrentUser):
    require_org(db, user, org_id, admin=True)
    return [{"id": actor.id, "email": actor.email, "name": actor.name, "role": member.role}
        for actor, member in db.execute(select(User, Membership).join(Membership).where(Membership.organization_id == org_id))]


def lock_organization(db, org_id):
    org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    db.execute(update(Organization).where(Organization.id == org_id).values(name=org.name))


def protect_last_admin(db, org_id, member, next_role):
    if member and member.role == "admin" and next_role != "admin":
        count = db.scalar(select(func.count()).select_from(Membership).where(
            Membership.organization_id == org_id, Membership.role == "admin"))
        if count <= 1:
            raise HTTPException(409, "组织必须保留至少一名管理员")


@router.put("/organizations/{org_id}/members")
def set_member(org_id: str, body: MemberRequest, db: DB, user: CurrentUser):
    require_org(db, user, org_id, admin=True)
    lock_organization(db, org_id)
    require_org(db, user, org_id, admin=True)
    actor = db.scalar(select(User).where(User.email == body.email.lower(), User.disabled.is_(False)))
    if not actor:
        raise HTTPException(404, "用户尚未注册")
    member = db.get(Membership, (org_id, actor.id))
    protect_last_admin(db, org_id, member, body.role)
    if not member:
        db.execute(delete(KnowledgeBaseGrant).where(KnowledgeBaseGrant.user_id == actor.id,
            KnowledgeBaseGrant.knowledge_base_id.in_(select(KnowledgeBase.id).where(KnowledgeBase.organization_id == org_id))))
        member = Membership(organization_id=org_id, user_id=actor.id)
        db.add(member)
    member.role = body.role
    audit(db, user.id, "membership.set_role." + body.role, actor.id, org_id)
    db.commit()
    return {"id": actor.id, "email": actor.email, "role": member.role}


@router.delete("/organizations/{org_id}/members/{user_id}", status_code=204)
def remove_member(org_id: str, user_id: str, db: DB, user: CurrentUser):
    require_org(db, user, org_id, admin=True)
    lock_organization(db, org_id)
    require_org(db, user, org_id, admin=True)
    member = db.get(Membership, (org_id, user_id))
    if not member:
        raise HTTPException(404, "成员不存在")
    protect_last_admin(db, org_id, member, None)
    db.execute(delete(KnowledgeBaseGrant).where(KnowledgeBaseGrant.user_id == user_id,
        KnowledgeBaseGrant.knowledge_base_id.in_(select(KnowledgeBase.id).where(KnowledgeBase.organization_id == org_id))))
    db.delete(member)
    # Corporate offboarding also revokes existing sessions; personal data is retained.
    db.execute(update(AuthSession).where(AuthSession.user_id == user_id).values(revoked=True))
    audit(db, user.id, "membership.remove", user_id, org_id)
    db.commit()


@router.get("/knowledge-bases/{kb_id}/members")
def kb_members(kb_id: str, db: DB, user: CurrentUser):
    kb = require_kb(db, user, kb_id, "admin")
    if not kb.organization_id:
        return []
    return [{"id": actor.id, "email": actor.email, "role": grant.role} for actor, grant in db.execute(
        select(User, KnowledgeBaseGrant).join(KnowledgeBaseGrant).where(KnowledgeBaseGrant.knowledge_base_id == kb_id))]


@router.put("/knowledge-bases/{kb_id}/members")
def set_kb_member(kb_id: str, body: MemberRequest, db: DB, user: CurrentUser):
    kb = require_kb(db, user, kb_id, "admin")
    if not kb.organization_id or kb.visibility != "restricted":
        raise HTTPException(422, "仅指定成员可见的团队知识库支持单独授权")
    lock_organization(db, kb.organization_id)
    require_kb(db, user, kb_id, "admin")
    actor = db.scalar(select(User).join(Membership).where(User.email == body.email.lower(),
        Membership.organization_id == kb.organization_id, User.disabled.is_(False)))
    if not actor:
        raise HTTPException(404, "用户尚未加入此组织")
    grant = db.get(KnowledgeBaseGrant, (kb_id, actor.id))
    if not grant:
        grant = KnowledgeBaseGrant(knowledge_base_id=kb_id, user_id=actor.id)
        db.add(grant)
    grant.role = body.role
    audit(db, user.id, "knowledge_base.grant." + body.role, kb_id, kb.organization_id)
    db.commit()
    return {"id": actor.id, "role": body.role}


@router.delete("/knowledge-bases/{kb_id}/members/{user_id}", status_code=204)
def remove_kb_member(kb_id: str, user_id: str, db: DB, user: CurrentUser):
    kb = require_kb(db, user, kb_id, "admin")
    if not kb.organization_id or kb.visibility != "restricted":
        raise HTTPException(422, "此知识库不使用单独授权")
    lock_organization(db, kb.organization_id)
    require_kb(db, user, kb_id, "admin")
    grant = db.get(KnowledgeBaseGrant, (kb_id, user_id))
    if not grant:
        raise HTTPException(404, "授权不存在")
    db.delete(grant)
    audit(db, user.id, "knowledge_base.revoke", kb_id, kb.organization_id)
    db.commit()


@router.get("/audit-events")
def audit_events(db: DB, user: CurrentUser, organization_id: str | None = None, limit: int = 100):
    query = select(AuditEvent)
    if organization_id:
        require_org(db, user, organization_id, admin=True)
        query = query.where(AuditEvent.organization_id == organization_id)
    else:
        query = query.where(AuditEvent.actor_id == user.id)
    return [{"id": row.id, "actor_id": row.actor_id, "action": row.action, "resource_id": row.resource_id,
        "request_id": row.request_id, "created_at": row.created_at} for row in db.scalars(
        query.order_by(AuditEvent.created_at.desc()).limit(max(1, min(limit, 500))))]


def get_document(db, user, document_id, action="read"):
    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    require_kb(db, user, doc.knowledge_base_id, action)
    return doc


@router.get("/documents/{document_id}/versions")
def versions(document_id: str, db: DB, user: CurrentUser):
    doc = get_document(db, user, document_id)
    return [{"id": rev.id, "name": rev.name, "sha256": rev.sha256, "size_bytes": rev.size_bytes,
        "status": rev.status, "active": rev.id == doc.active_revision_id, "created_at": rev.created_at,
        "quality_report": rev.quality_report}
        for rev in db.scalars(select(DocumentRevision).where(DocumentRevision.document_id == document_id)
        .order_by(DocumentRevision.created_at.desc()).limit(100))]


@router.get("/documents/{document_id}/versions/{revision_id}/download")
def download_revision(document_id: str, revision_id: str, request: Request, db: DB, user: CurrentUser):
    from pathlib import Path

    from fastapi.responses import FileResponse
    doc = get_document(db, user, document_id)
    rev = db.get(DocumentRevision, revision_id)
    if not rev or rev.document_id != document_id:
        raise HTTPException(404, "版本不存在")
    path = Path(rev.stored_path).resolve()
    if not path.is_relative_to((request.app.state.settings.data_dir / "uploads").resolve()) or not path.is_file():
        raise HTTPException(404, "版本原文件不存在")
    kb = require_kb(db, user, doc.knowledge_base_id)
    audit(db, user.id, "document.version.download", revision_id, kb.organization_id)
    db.commit()
    return FileResponse(path, filename=rev.name, media_type="application/octet-stream")


@router.post("/documents/{document_id}/versions/{revision_id}/restore", status_code=202)
def restore_version(document_id: str, revision_id: str, request: Request, db: DB, user: CurrentUser):
    doc = get_document(db, user, document_id, "write")
    from app.services.lifecycle import reserve_ingestion
    reserve_ingestion(db, user, doc.knowledge_base_id, request.app.state.settings)
    if doc.status not in {"ready", "failed", "needs_review"}:
        raise HTTPException(409, "已有更新任务正在处理")
    rev = db.get(DocumentRevision, revision_id)
    if not rev or rev.document_id != document_id:
        raise HTTPException(404, "版本不存在")
    copy = DocumentRevision(document_id=document_id, name=rev.name, stored_path=rev.stored_path,
        sha256=rev.sha256, size_bytes=rev.size_bytes)
    db.add(copy)
    db.flush()
    job = IngestionJob(document_id=document_id, revision_id=copy.id)
    db.add(job)
    doc.status, doc.error = "queued", None
    audit(db, user.id, "document.restore", document_id,
          require_kb(db, user, doc.knowledge_base_id).organization_id)
    db.commit()
    return {"job_id": job.id, "revision_id": copy.id}


@router.patch("/documents/{document_id}/metadata")
def update_metadata(document_id: str, body: DocumentMetadataRequest, db: DB, user: CurrentUser):
    doc = get_document(db, user, document_id, "write")
    try:
        value = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00")) if body.expires_at else None
        if value and value.tzinfo is None:
            raise ValueError("timezone required")
        doc.expires_at = value.astimezone(UTC).replace(tzinfo=None) if value else None
    except ValueError:
        raise HTTPException(422, "expires_at 必须为包含时区的 ISO 时间")
    doc.source = body.source
    db.execute(update(KnowledgeBase).where(KnowledgeBase.id == doc.knowledge_base_id)
        .values(generation=KnowledgeBase.generation + 1))
    audit(db, user.id, "document.metadata", document_id,
          require_kb(db, user, doc.knowledge_base_id).organization_id)
    db.commit()
    return {"id": doc.id, "expires_at": doc.expires_at, "source": doc.source}


@router.post("/sources/sync")
async def sync_source(body: SourceSyncRequest, request: Request, db: DB, user: CurrentUser):
    from io import BytesIO

    from fastapi import UploadFile

    from app.api.routes import ALLOWED_EXTENSIONS, upload_document
    from app.services.admission import consume
    kb = require_kb(db, user, body.knowledge_base_id, "write")
    settings = request.app.state.settings
    consume(request.app.state.database, f"sync:{user.id}", 5, 60)
    if not settings.source_sync_root:
        raise HTTPException(503, "管理员尚未配置文档源目录")
    root = settings.source_sync_root.resolve()
    target = (root / body.relative_path).resolve()
    if not target.is_relative_to(root) or not target.exists():
        raise HTTPException(422, "路径必须位于配置的文档源目录内")
    files = []
    for scanned, path in enumerate([target] if target.is_file() else target.rglob("*"), 1):
        if scanned > 2000:
            raise HTTPException(422, "文档源目录过大，请选择更小的子目录")
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS:
            if not path.resolve().is_relative_to(root):
                raise HTTPException(422, "文档源包含越界符号链接")
            files.append(path)
            if len(files) > 100:
                raise HTTPException(422, "单次同步最多100个文件，请按子目录分批同步")
    rows = []
    for path in sorted(files):
        import hashlib
        source = "folder:" + path.relative_to(root).as_posix()
        if path.stat().st_size > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "文档源文件超过大小限制")
        import asyncio
        data = await asyncio.to_thread(path.read_bytes)
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "文档源文件超过大小限制")
        existing = db.scalar(select(Document).where(Document.knowledge_base_id == body.knowledge_base_id,
            Document.source == source))
        if existing and existing.sha256 == hashlib.sha256(data).hexdigest():
            rows.append({"document_id": existing.id, "unchanged": True})
            continue
        result = await upload_document(request, db, user, UploadFile(BytesIO(data), filename=path.name),
            body.knowledge_base_id, existing.id if existing else None)
        doc = db.get(Document, result["document"]["id"])
        if doc.source != source:
            db.execute(update(KnowledgeBase).where(KnowledgeBase.id == doc.knowledge_base_id)
                .values(generation=KnowledgeBase.generation + 1))
            audit(db, user.id, "document.source.sync", doc.id, kb.organization_id)
        doc.source = source
        db.commit()
        rows.append({"document_id": doc.id, "job_id": result["job_id"], "unchanged": False})
    return {"files": rows, "note": "增量导入与替换；源文件移除时保留知识库版本，需显式过期或删除"}


@router.get("/ready")
def ready(request: Request):
    from fastapi.responses import JSONResponse

    from app.services.observability import readiness
    checks = readiness(request.app.state.database, request.app.state.settings)
    return JSONResponse({"ready": all(checks.values()), "checks": checks}, status_code=200 if all(checks.values()) else 503)


@router.get("/metrics")
def prometheus_metrics(request: Request):
    from app.services.observability import metrics
    token = request.app.state.settings.metrics_token
    if not token:
        raise HTTPException(404)
    if not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
        raise HTTPException(401)
    data, mime = metrics(request.app.state.database)
    return Response(content=data, headers={"Content-Type": mime})


@router.get("/ready/models")
def models_ready(request: Request, user: CurrentUser):
    from app.health import model_checks
    return {"checks": model_checks(request.app.state.settings), "scope": "API connectivity and model listing; no inference"}


@router.get("/operations")
def operations(request: Request, db: DB, user: CurrentUser):
    from app.services.access import allowed_kbs
    from app.services.operations import effective_settings, is_platform_admin
    settings = effective_settings(db, request.app.state.settings, user.id)
    jobs = db.execute(select(IngestionJob.status, func.count()).join(Document).join(KnowledgeBase)
        .where(allowed_kbs(user.id)).group_by(IngestionJob.status)).all()
    return {"ingestion_jobs": dict(jobs), "limits": {
        key: getattr(settings, key) for key in ["user_storage_mb", "max_pending_jobs",
            "max_active_chats", "global_active_chats", "user_daily_model_requests"]},
        "my_audit_events": db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.actor_id == user.id)),
        "is_platform_admin": is_platform_admin(user, request.app.state.settings)}
