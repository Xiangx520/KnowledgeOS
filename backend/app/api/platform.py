"""Platform-only quota controls and recoverable task operations."""
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field
from sqlalchemy import select

from app.api.dependencies import DB, CurrentUser
from app.models import (
    CleanupTask,
    Document,
    DocumentRevision,
    EvaluationRun,
    IngestionJob,
    OperationalPolicy,
    User,
    utcnow,
)
from app.schemas import StrictRequest
from app.services.access import audit
from app.services.operations import LIMITS, effective_settings, require_platform_admin

router = APIRouter(prefix="/api/platform")


class Quotas(StrictRequest):
    user_storage_mb: int = Field(ge=1, le=10485760)
    max_pending_jobs: int = Field(ge=1, le=10000)
    max_active_chats: int = Field(ge=1, le=32)
    global_active_chats: int = Field(ge=1, le=128)
    user_daily_model_requests: int = Field(ge=1, le=10000000)


@router.get("/quotas")
def quotas(request: Request, db: DB, user: CurrentUser, user_id: str | None = None):
    require_platform_admin(user, request.app.state.settings)
    if user_id and not db.get(User, user_id):
        raise HTTPException(404, "账号不存在")
    settings = effective_settings(db, request.app.state.settings, user_id)
    row = db.get(OperationalPolicy, "user:" + user_id if user_id else "global")
    return {"limits": {key: getattr(settings, key) for key in LIMITS}, "overridden": bool(row)}


@router.put("/quotas")
def save_quotas(body: Quotas, request: Request, db: DB, user: CurrentUser, user_id: str | None = None):
    require_platform_admin(user, request.app.state.settings)
    if user_id and not db.get(User, user_id):
        raise HTTPException(404, "账号不存在")
    scope = "user:" + user_id if user_id else "global"
    values = body.model_dump()
    if user_id:
        values.pop("global_active_chats")
    row = db.get(OperationalPolicy, scope)
    if not row:
        row = OperationalPolicy(scope=scope, updated_by=user.id)
        db.add(row)
    row.limits, row.updated_by, row.updated_at = values, user.id, utcnow()
    audit(db, user.id, "platform.quotas.update", scope)
    db.commit()
    return quotas(request, db, user, user_id)


@router.delete("/quotas", status_code=204)
def reset_quotas(request: Request, db: DB, user: CurrentUser, user_id: str):
    require_platform_admin(user, request.app.state.settings)
    row = db.get(OperationalPolicy, "user:" + user_id)
    if row:
        db.delete(row)
    audit(db, user.id, "platform.quotas.reset", user_id)
    db.commit()


@router.get("/users")
def users(request: Request, db: DB, user: CurrentUser):
    require_platform_admin(user, request.app.state.settings)
    return [{"id": row.id, "name": row.name, "email": row.email} for row in db.scalars(
        select(User).order_by(User.email).limit(1000))]


@router.get("/jobs")
def jobs(request: Request, db: DB, user: CurrentUser, kind: Literal["ingestion", "evaluation", "cleanup"] = "ingestion",
         status: str | None = None, limit: int = 50):
    require_platform_admin(user, request.app.state.settings)
    model = {"ingestion": IngestionJob, "evaluation": EvaluationRun, "cleanup": CleanupTask}[kind]
    query = select(model).order_by(model.created_at.desc()).limit(max(1, min(limit, 100)))
    if status:
        query = query.where(model.status == status)
    result = []
    for row in db.scalars(query):
        doc = db.get(Document, row.document_id) if kind == "ingestion" else None
        result.append({"id": row.id, "kind": kind, "name": doc.name if doc else getattr(row, "name", "文件与索引清理"),
            "document_id": doc.id if doc else None, "status": row.status, "progress": getattr(row, "progress", None),
            "stage": getattr(row, "stage", None), "processed_pages": getattr(row, "processed_pages", 0),
            "total_pages": getattr(row, "total_pages", 0), "attempts": getattr(row, "attempts", 0),
            "error": row.error, "created_at": row.created_at, "lease_expires_at": row.lease_expires_at})
    return result


@router.post("/jobs/{kind}/{job_id}/{action}")
def control_job(kind: Literal["ingestion", "evaluation", "cleanup"], job_id: str,
                action: Literal["cancel", "retry"], request: Request, db: DB, user: CurrentUser):
    require_platform_admin(user, request.app.state.settings)
    model = {"ingestion": IngestionJob, "evaluation": EvaluationRun, "cleanup": CleanupTask}[kind]
    # Match ingestion's lock order so deletion/cancellation cannot deadlock activation.
    row = db.get(model, job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if kind == "ingestion" and action == "retry":
        from app.models import KnowledgeBase
        owner_id = db.scalar(select(KnowledgeBase.user_id).join(Document).where(Document.id == row.document_id))
        if owner_id:
            db.scalar(select(User.id).where(User.id == owner_id).with_for_update())
    doc = db.scalar(select(Document).where(Document.id == row.document_id).with_for_update()) if kind == "ingestion" else None
    row = db.scalar(select(model).where(model.id == job_id).with_for_update().execution_options(populate_existing=True))
    if action == "cancel":
        if kind == "cleanup":
            raise HTTPException(422, "清理任务必须保留以完成数据删除，可使用重试")
        if row.status not in {"queued", "processing", "running"}:
            raise HTTPException(409, "此任务已结束")
        row.status, row.lease_token, row.lease_expires_at, row.error = "cancelled", None, None, "平台管理员已取消任务"
        if doc:
            row.stage = "已取消"
            doc.status, doc.error = ("ready" if doc.active_revision_id else "cancelled"), None
            revision = db.get(DocumentRevision, row.revision_id)
            if revision and revision.id != doc.active_revision_id:
                revision.status = "cancelled"
            if row.revision_id and row.revision_id != doc.active_revision_id:
                db.add(CleanupTask(document_id=doc.id, revision_id=row.revision_id))
    else:
        if row.status in {"processing", "running"} and row.lease_expires_at and row.lease_expires_at > utcnow():
            raise HTTPException(409, "任务仍在运行，请先取消再重试")
        if row.status not in {"failed", "needs_review", "cancelled", "queued", "processing", "running"}:
            raise HTTPException(409, "已完成任务无需重试；更新文档请使用重新索引")
        if kind == "ingestion":
            if not doc:
                raise HTTPException(404, "文档已删除")
            # The job owner remains the billing owner; admin status does not bypass quota.
            from app.models import KnowledgeBase
            from app.services.lifecycle import reserve_ingestion
            owner = db.get(User, db.get(KnowledgeBase, doc.knowledge_base_id).user_id)
            if row.status in {"processing", "queued"}:
                row.status = "cancelled"
                db.flush()
            reserve_ingestion(db, owner, doc.knowledge_base_id, request.app.state.settings)
            source = db.get(DocumentRevision, row.revision_id)
            revision = DocumentRevision(document_id=doc.id, name=source.name if source else doc.name,
                stored_path=source.stored_path if source else doc.stored_path,
                sha256=source.sha256 if source else doc.sha256, size_bytes=source.size_bytes if source else doc.size_bytes)
            db.add(revision)
            db.flush()
            row.revision_id, row.status, row.progress, row.stage = revision.id, "queued", 0, "等待处理"
            row.error, row.available_at, row.lease_token, row.lease_expires_at = None, utcnow(), None, None
            row.attempts, row.processed_pages, row.total_pages = 0, 0, 0
            doc.status, doc.error = "queued", None
        elif kind == "evaluation":
            from app.services.admission import consume
            settings = effective_settings(db, request.app.state.settings, row.user_id)
            consume(request.app.state.database, "model-budget:" + row.user_id,
                    settings.user_daily_model_requests, 86400, cost=len(row.cases) * (6 if row.use_ragas else 1))
            row.status, row.lease_token, row.lease_expires_at, row.error = "queued", None, None, None
        else:
            row.available_at = utcnow()
            if row.lease_expires_at and row.lease_expires_at > utcnow() + timedelta(seconds=1):
                raise HTTPException(409, "清理任务仍持有租约")
    audit(db, user.id, "platform.job." + action, job_id)
    db.commit()
    return {"id": row.id, "status": row.status}
