from importlib.util import find_spec

from app.config import get_settings
from app.services.answering import llm_ready


def capabilities() -> dict:
    s = get_settings()
    return {"mode": s.environment, "embedding_provider": s.embedding_provider,
        "embedding_model": s.embedding_model if s.embedding_provider != "hash" else "deterministic-hash-demo",
        "llm_provider": s.llm_model if llm_ready() else "extractive", "llm_model": s.llm_model,
        "llm_backend": s.llm_provider if llm_ready() else "extractive",
        "reranker": s.reranker_model or "disabled", "reranker_provider": s.reranker_provider,
        "vector_store": "qdrant-server" if s.qdrant_url else "qdrant-local",
        "queue": "redis+sql-durable" if s.redis_url else "sql-durable-local", "max_upload_mb": s.max_upload_mb,
        "features": {"hybrid_retrieval": True, "bounded_agent": True, "ocr_configured": s.ocr_enabled,
            "organization_roles": True, "revocable_sessions": True, "oidc_configured": bool(s.oidc_issuer),
            "persistent_bm25": True, "document_versions": True, "async_evaluations": True,
            "durable_cleanup": True, "provider_streaming": True, "citation_verifier": s.citation_verifier,
            "folder_sync_configured": bool(s.source_sync_root), "metrics_configured": bool(s.metrics_token),
            "vision_configured": bool(s.vision_model and (s.llm_provider == "ollama" or s.llm_api_key)), "cross_encoder_configured": bool(s.reranker_model),
            "ragas_installed": find_spec("ragas") is not None,
            "ragas_ready": find_spec("ragas") is not None and llm_ready() and s.embedding_provider != "hash",
            "semantic_embeddings": s.embedding_provider != "hash", "history": True, "source_citations": True}}
