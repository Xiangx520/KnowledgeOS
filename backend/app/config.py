from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_", env_file=("../.env", ".env"), extra="ignore"
    )

    database_url: str = "sqlite:///./data/rag.db"
    data_dir: Path = Path("./data")
    secret_key: str = "development-only-change-this-secret-before-production"
    environment: str = "development"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    max_upload_mb: int = Field(default=50, ge=1, le=50)
    redis_url: str = ""
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    embedding_provider: str = "hash"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dimension: int = Field(default=384, ge=32)
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_num_ctx: int = Field(default=4096, ge=128)
    embedding_batch_size: int = Field(default=8, ge=1, le=256)
    llm_provider: str = "openai"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    ollama_num_ctx: int = Field(default=16384, ge=1024)
    model_timeout: int = Field(default=180, ge=5, le=1800)
    max_output_tokens: int = Field(default=2048, ge=64)
    qdrant_collection_prefix: str = Field(default="rag_assistant", min_length=1, max_length=64,
                                        pattern=r"^[A-Za-z0-9_-]+$")
    vision_model: str = ""
    reranker_model: str = ""
    reranker_provider: str = "local"
    reranker_api_url: str = ""
    reranker_api_key: str = ""
    ocr_enabled: bool = False
    chunk_size: int = Field(default=800, ge=100, le=10000)
    chunk_overlap: int = Field(default=120, ge=0)
    context_enrichment_enabled: bool = False
    context_enrichment_max_calls: int = Field(default=16, ge=0, le=256)
    context_enrichment_max_bytes: int = Field(default=12000, ge=1024, le=32000)
    context_enrichment_timeout: int = Field(default=30, ge=5, le=120)
    cache_ttl: int = Field(default=3600, ge=0)
    agent_max_rewrites: int = Field(default=2, ge=0, le=5)
    # Bounded agentic retrieval. Disabled by default; mode=agent falls back to
    # the legacy intent-retrieval path when this is false or no model is ready.
    agent_loop_enabled: bool = False
    agent_max_steps: int = Field(default=6, ge=1, le=32)
    agent_max_tool_calls: int = Field(default=8, ge=1, le=32)
    agent_max_model_calls: int = Field(default=12, ge=1, le=64)
    agent_max_total_tokens: int = Field(default=24000, ge=512)
    agent_deadline_seconds: int = Field(default=120, ge=5, le=1800)
    agent_max_evidence_chunks: int = Field(default=24, ge=1, le=100)
    agent_max_dynamic_tasks: int = Field(default=8, ge=0, le=32)
    agent_max_recovery_rounds: int = Field(default=1, ge=0, le=5)
    agent_min_search_coverage: float = Field(default=0.25, ge=0, le=1)
    agent_max_query_chars: int = Field(default=600, ge=32, le=4000)
    agent_max_observation_chars: int = Field(default=6000, ge=400, le=32000)
    agent_max_evidence_chars: int = Field(default=4000, ge=400, le=32000)
    agent_max_ledger_chars: int = Field(default=48000, ge=4000, le=200000)
    agent_generation_reserve_model_calls: int = Field(default=2, ge=1, le=4)
    agent_generation_reserve_tokens: int = Field(default=1500, ge=256)
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    job_lease_seconds: int = Field(default=600, ge=30)
    token_lifetime_seconds: int = Field(default=86400, ge=60)
    auto_migrate: bool = True
    allow_registration: bool = True
    auth_rate_per_minute: int = Field(default=20, ge=1)
    chat_rate_per_minute: int = Field(default=30, ge=1)
    user_storage_mb: int = Field(default=1024, ge=1)
    max_pending_jobs: int = Field(default=20, ge=1)
    max_active_chats: int = Field(default=2, ge=1, le=32)
    global_active_chats: int = Field(default=4, ge=1, le=128)
    worker_role: str = "all"
    user_daily_model_requests: int = Field(default=500, ge=1)
    worker_max_attempts: int = Field(default=3, ge=1, le=10)
    metrics_token: str = ""
    cookie_secure: bool = False
    public_base_url: str = "http://localhost:5173"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:5173/api/auth/oidc/callback"
    citation_verifier: str = "numeric"  # numeric | llm; numeric is explicitly a partial check
    verifier_model: str = ""
    evidence_min_coverage: float = Field(default=0.25, ge=0, le=1)
    source_sync_root: Path | None = None
    platform_admin_emails: list[str] = []
    pdf_vision_all_pages: bool = False
    ocr_timeout: int = Field(default=60, ge=5, le=300)

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.reranker_provider not in {"local", "dashscope"}:
            raise ValueError("reranker_provider must be local or dashscope")
        if self.reranker_model and self.reranker_provider == "dashscope":
            if not self.reranker_api_url.startswith("https://"):
                raise ValueError("DashScope reranking requires an HTTPS RAG_RERANKER_API_URL")
            if not (self.reranker_api_key or self.llm_api_key):
                raise ValueError("DashScope reranking requires RAG_RERANKER_API_KEY or RAG_LLM_API_KEY")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if self.ollama_num_ctx <= self.max_output_tokens + 2048:
            raise ValueError("ollama_num_ctx must leave at least 2048 tokens beyond max_output_tokens")
        if self.redis_url and not self.qdrant_url:
            raise ValueError("A standalone Redis worker requires a Qdrant server; local Qdrant is single-process")
        if self.citation_verifier not in {"numeric", "llm"}:
            raise ValueError("citation_verifier must be numeric or llm")
        if self.worker_role not in {"all", "ingestion", "evaluation"}:
            raise ValueError("worker_role must be all, ingestion or evaluation")
        if self.oidc_issuer and (not self.oidc_issuer.startswith("https://") or not self.oidc_client_id):
            raise ValueError("OIDC requires an HTTPS issuer and client_id")
        if self.agent_generation_reserve_model_calls > self.agent_max_model_calls:
            raise ValueError("agent_generation_reserve_model_calls exceeds agent_max_model_calls")
        if self.agent_generation_reserve_tokens >= self.agent_max_total_tokens:
            raise ValueError("agent_generation_reserve_tokens must be smaller than agent_max_total_tokens")
        if self.environment.lower() == "production":
            if len(self.secret_key) < 32 or self.secret_key.startswith("development-only"):
                raise ValueError("Production requires a unique RAG_SECRET_KEY of at least 32 characters")
            if self.database_url.startswith("sqlite"):
                raise ValueError("Production requires PostgreSQL via RAG_DATABASE_URL")
            if not self.redis_url:
                raise ValueError("Production requires Redis via RAG_REDIS_URL and a standalone worker")
            if not self.qdrant_url:
                raise ValueError("Production requires a Qdrant server via RAG_QDRANT_URL")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
