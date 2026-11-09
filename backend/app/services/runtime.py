from importlib.util import find_spec

from app.config import get_settings
from app.services.answering import llm_ready


def capabilities() -> dict:
    s = get_settings()
    model_ready = llm_ready(s)
    agent_available = bool(s.agent_loop_enabled and model_ready)
    return {"mode": s.environment, "embedding_provider": s.embedding_provider,
        "embedding_model": s.embedding_model if s.embedding_provider != "hash" else "deterministic-hash-demo",
        "llm_provider": s.llm_model if llm_ready() else "extractive", "llm_model": s.llm_model,
        "llm_backend": s.llm_provider if llm_ready() else "extractive",
        "reranker": s.reranker_model or "disabled", "reranker_provider": s.reranker_provider,
        "vector_store": "qdrant-server" if s.qdrant_url else "qdrant-local",
        "queue": "redis+sql-durable" if s.redis_url else "sql-durable-local", "max_upload_mb": s.max_upload_mb,
        "agent": {"version": "agent-v1", "enabled": s.agent_loop_enabled, "available": agent_available,
            "mode": "loop" if agent_available else "legacy", "requires_model": True,
            "semantic_verifier": s.citation_verifier == "llm",
            "budget": {"max_steps": s.agent_max_steps, "max_tool_calls": s.agent_max_tool_calls,
                       "max_model_calls": s.agent_max_model_calls,
                       "max_total_tokens": s.agent_max_total_tokens,
                       "deadline_seconds": s.agent_deadline_seconds,
                       "max_evidence_chunks": s.agent_max_evidence_chunks}},
        "features": {"hybrid_retrieval": True, "bounded_agent": agent_available, "ocr_configured": s.ocr_enabled,
            "organization_roles": True, "revocable_sessions": True, "oidc_configured": bool(s.oidc_issuer),
            "persistent_bm25": True, "document_versions": True, "async_evaluations": True,
            "durable_cleanup": True, "provider_streaming": True, "citation_verifier": s.citation_verifier,
            "folder_sync_configured": bool(s.source_sync_root), "metrics_configured": bool(s.metrics_token),
            "vision_configured": bool(s.vision_model and (s.llm_provider == "ollama" or s.llm_api_key)), "cross_encoder_configured": bool(s.reranker_model),
            "ragas_installed": find_spec("ragas") is not None,
            "ragas_ready": find_spec("ragas") is not None and llm_ready() and s.embedding_provider != "hash",
            "semantic_embeddings": s.embedding_provider != "hash", "history": True, "source_citations": True}}
