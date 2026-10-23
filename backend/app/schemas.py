from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictRequest):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=8, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return value.lower()


class RegisterRequest(LoginRequest):
    name: str = Field(min_length=1, max_length=100)


class KnowledgeBaseRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    organization_id: str | None = None
    visibility: Literal["organization", "restricted"] = "organization"


class OrganizationRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=120)


class MemberRequest(StrictRequest):
    email: str = Field(min_length=3, max_length=254)
    role: Literal["admin", "editor", "reader"] = "reader"


class DocumentMetadataRequest(StrictRequest):
    expires_at: str | None = Field(default=None, max_length=64)
    source: str = Field(default="upload", max_length=2000)


class SourceSyncRequest(StrictRequest):
    knowledge_base_id: str
    relative_path: str = Field(min_length=1, max_length=500)


class ChatRequest(StrictRequest):
    question: str = Field(min_length=1, max_length=8000)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    top_k: int = Field(default=5, ge=1, le=20)
    mode: Literal["standard", "agent"] = "standard"
    conversation_id: str | None = None
    request_id: str | None = Field(default=None, min_length=8, max_length=64)


class EvaluationTurn(StrictRequest):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class EvaluationCase(StrictRequest):
    question: str = Field(min_length=1, max_length=8000)
    reference: str = Field(default="", max_length=32000)
    expected_document_ids: list[str] = Field(default_factory=list, max_length=100)
    message_id: str | None = None
    history: list[EvaluationTurn] = Field(default_factory=list, max_length=40)
    conversation_key: str | None = Field(default=None, max_length=100)
    expected_action: Literal["retrieve", "clarify"] = "retrieve"
    expected_chunk_ids: list[str] = Field(default_factory=list, max_length=100)
    expected_subject: str | None = Field(default=None, max_length=300)
    expected_constraints: list[str] = Field(default_factory=list, max_length=12)
    expected_task_count: int | None = Field(default=None, ge=0, le=4)


class EvaluationRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=120)
    cases: list[EvaluationCase] = Field(min_length=1, max_length=100)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    use_ragas: bool = False


class ComparisonRequest(StrictRequest):
    cases: list[EvaluationCase] = Field(min_length=1, max_length=100)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    kind: Literal["retrieval", "query_policy"] = "retrieval"
    use_model: bool = False
    mode: Literal["standard", "agent"] = "standard"
