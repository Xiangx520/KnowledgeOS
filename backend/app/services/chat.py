"""Idempotent chat runs, conversation ordering and durable run metadata."""
import hashlib
import json
import threading
import time
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.models import AuthSession, ChatRequestRun, Conversation, KnowledgeBase, Message, User, new_id, utcnow
from app.services.access import allowed_kbs, audit
from app.services.admission import capacity, consume
from app.services.corpus import CorpusScope


def run_chat(body, db, user, request, emit=None, cancelled=None):
    from app.services.answering import PROMPT_VERSION, answer_question
    settings, database = request.app.state.settings, request.app.state.database
    from app.services.operations import effective_settings
    settings = effective_settings(db, settings, user.id)
    key = body.request_id or new_id()
    digest = hashlib.sha256(json.dumps(body.model_dump(exclude={"request_id"}), sort_keys=True).encode()).hexdigest()
    conversation = db.scalar(select(Conversation).where(Conversation.id == body.conversation_id,
        Conversation.user_id == user.id)) if body.conversation_id else None
    if body.conversation_id and not conversation:
        raise HTTPException(404, "会话不存在")
    scope = CorpusScope(db, user, body.knowledge_base_ids)
    existing = db.scalar(select(ChatRequestRun).where(ChatRequestRun.user_id == user.id, ChatRequestRun.request_key == key))
    if existing:
        if existing.body_hash != digest:
            raise HTTPException(409, "相同请求 ID 不能用于不同问题")
        if existing.status == "completed":
            result = existing.result
            citations = result.get("citations", [])
            allowed = scope.hydrate([c["chunk_id"] for c in citations])
            if any(c["chunk_id"] not in allowed for c in citations):
                raise HTTPException(409, "该请求的证据版本或授权已变化，请使用新的请求 ID")
            return {**result, "idempotent_replay": True}
        if existing.status == "running" and existing.expires_at > utcnow():
            raise HTTPException(409, "同一请求正在处理中，请稍后查询或重试")
        db.delete(existing)
        db.commit()
    consume(database, f"chat:{user.id}", settings.chat_rate_per_minute)
    consume(database, f"model-budget:{user.id}", settings.user_daily_model_requests, 86400)
    lease, ttl = new_id(), settings.model_timeout * 10 + 180
    with capacity(database, "global", settings.global_active_chats, ttl), capacity(database, user.id, settings.max_active_chats, ttl):
        run = ChatRequestRun(user_id=user.id, request_key=key, body_hash=digest,
                             expires_at=utcnow() + timedelta(seconds=ttl))
        db.add(run)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "同一请求正在处理中")
        if conversation is None:
            conversation = Conversation(user_id=user.id, title=body.question[:100])
            db.add(conversation)
            db.flush()
        changed = db.execute(update(Conversation).where(Conversation.id == conversation.id,
            or_(Conversation.lease_expires_at.is_(None), Conversation.lease_expires_at < utcnow()))
            .values(lease_token=lease, lease_expires_at=utcnow() + timedelta(seconds=ttl)))
        if not changed.rowcount:
            db.rollback()
            raise HTTPException(409, "此会话正在回答上一条问题，请等待完成")
        db.commit()
        conversation_id, run_id = conversation.id, run.id
        done = threading.Event()

        def heartbeat():
            while not done.wait(20):
                with database.session() as session:
                    session.execute(update(Conversation).where(Conversation.id == conversation_id,
                        Conversation.lease_token == lease).values(lease_expires_at=utcnow() + timedelta(seconds=ttl)))
                    session.execute(update(ChatRequestRun).where(ChatRequestRun.id == run_id,
                        ChatRequestRun.status == "running").values(expires_at=utcnow() + timedelta(seconds=ttl)))
                    session.commit()
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            from app.services.intent import read_state, rule_plan
            latest = db.scalar(select(Message).where(Message.conversation_id == conversation_id, Message.role == "user")
                .order_by(Message.created_at.desc(), Message.id.desc()).limit(1))
            saved = (latest.run_metadata or {}).get("intent_state") if latest else None
            state = read_state(saved)
            if latest and saved is None:
                # Bootstrap pre-upgrade conversations from user turns only, without model calls.
                turns = db.scalars(select(Message).where(Message.conversation_id == conversation_id, Message.role == "user")
                    .order_by(Message.created_at, Message.id).execution_options(yield_per=100))
                for turn in turns:
                    state = rule_plan(turn.content, state, turn.id).state
            question_id = new_id()
            kwargs = {"top_k": body.top_k, "mode": body.mode, "intent_state": state.model_dump(), "message_id": question_id}
            if emit is not None:
                last_checked = 0
                def secure_emit(event, value):
                    nonlocal last_checked
                    if time.monotonic() - last_checked >= 1:
                        with database.session() as guard:
                            login = guard.get(AuthSession, request.state.session_id)
                            actor = guard.get(User, user.id)
                            accessible = set(guard.scalars(select(KnowledgeBase.id).where(allowed_kbs(user.id))).all())
                            if not login or login.revoked or login.expires_at <= utcnow() or actor.disabled:
                                raise HTTPException(401, "登录状态已被撤销")
                            if set(scope.kb_ids) - accessible:
                                raise HTTPException(403, "知识库权限已变化，已停止生成")
                        last_checked = time.monotonic()
                    emit(event, value)
                kwargs.update(emit=secure_emit, cancelled=cancelled)
            result = answer_question(body.question, scope, **kwargs)
            if cancelled and cancelled():
                raise HTTPException(499, "请求已取消")
            # Refresh authorization after the long model call, before returning shared knowledge.
            db.expire_all()
            login = db.get(AuthSession, request.state.session_id)
            actor = db.get(User, user.id)
            if not login or login.revoked or login.expires_at <= utcnow() or actor.disabled:
                raise HTTPException(401, "登录状态已被撤销")
            fresh_scope = CorpusScope(db, user, body.knowledge_base_ids)
            citations = result.get("citations", [])
            valid = fresh_scope.hydrate([c["chunk_id"] for c in citations])
            if any(c["chunk_id"] not in valid for c in citations):
                raise HTTPException(409, "证据版本或访问权限在回答过程中发生变化，请重新提问")
            owned = db.execute(update(Conversation).where(Conversation.id == conversation_id,
                Conversation.lease_token == lease).values(lease_expires_at=utcnow() + timedelta(seconds=ttl)))
            active_run = db.scalar(select(ChatRequestRun.id).where(ChatRequestRun.id == run_id,
                ChatRequestRun.status == "running").with_for_update())
            if not owned.rowcount or not active_run:
                raise HTTPException(409, "请求或会话租约已变化，当前答案不会覆盖后续请求")
            metadata = {"provider": result.get("provider"), "usage": result.get("usage", {}),
                "prompt_version": PROMPT_VERSION, "embedding_model": settings.embedding_model,
                "reranker_model": settings.reranker_model, "request_id": key,
                "timings_ms": result.get("timings_ms", {}), "verification": result.get("verification", {}),
                "intent": result.get("intent"), "task_results": result.get("task_results", [])}
            last = db.scalar(select(func.max(Message.created_at)).where(Message.conversation_id == conversation_id))
            timestamp = max(utcnow(), last + timedelta(microseconds=1)) if last else utcnow()
            db.add(Message(id=question_id, conversation_id=conversation_id, role="user", content=body.question,
                run_metadata={"intent_state": result.get("intent", {}).get("state", state.model_dump())}, created_at=timestamp))
            db.flush()
            assistant = Message(conversation_id=conversation_id, role="assistant", content=result["answer"],
                citations=citations, trace=result.get("trace", []), run_metadata=metadata, created_at=timestamp + timedelta(microseconds=1))
            db.add(assistant)
            db.flush()
            result.update(conversation_id=conversation_id, message_id=assistant.id, request_id=key)
            db.execute(update(ChatRequestRun).where(ChatRequestRun.id == run_id).values(status="completed", result=result))
            audit(db, user.id, "chat.complete", conversation_id, request_id=key)
            db.commit()
            return result
        except BaseException:
            db.rollback()
            db.execute(update(ChatRequestRun).where(ChatRequestRun.id == run_id).values(status="failed"))
            db.commit()
            raise
        finally:
            done.set()
            thread.join(timeout=1)
            db.execute(update(Conversation).where(Conversation.id == conversation_id, Conversation.lease_token == lease)
                       .values(lease_token=None, lease_expires_at=None))
            db.commit()
