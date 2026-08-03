"""Pydantic schemas for the Jarvis FastAPI contract."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from agents.agent_api.app.user_context.identity import TelegramIdentity


class LegacyIdentityInput(BaseModel):
    provider: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    subject: str = Field(..., min_length=1)
    username: Optional[str] = None
    display_name: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_subject(self) -> "LegacyIdentityInput":
        normalized_subject = self.subject.strip()
        if not normalized_subject:
            raise ValueError("identity subject must not be empty")
        self.subject = normalized_subject
        return self


class TelegramIdentityInput(BaseModel):
    telegram_id: int = Field(..., gt=0)
    username: Optional[str] = None

    def to_runtime_identity(self) -> TelegramIdentity:
        return TelegramIdentity(**self.model_dump())


class IdentityRequestMixin(BaseModel):
    telegram_identity: Optional[TelegramIdentityInput] = None
    identity: Optional[LegacyIdentityInput] = Field(
        default=None, json_schema_extra={"deprecated": True}
    )
    telegram_user_id: Optional[int] = Field(
        default=None, json_schema_extra={"deprecated": True}
    )
    telegram_username: Optional[str] = Field(
        default=None, json_schema_extra={"deprecated": True}
    )
    telegram_first_name: Optional[str] = Field(
        default=None, json_schema_extra={"deprecated": True}
    )

    @model_validator(mode="after")
    def normalize_legacy_identity(self) -> "IdentityRequestMixin":
        telegram_user_id = self.__dict__.get("telegram_user_id")
        telegram_username = self.__dict__.get("telegram_username")
        candidates: List[TelegramIdentityInput] = []
        if self.telegram_identity is not None:
            candidates.append(self.telegram_identity)

        if self.identity is not None:
            if self.identity.provider != "telegram":
                raise ValueError("only Telegram identities are supported")
            try:
                generic_telegram_id = int(self.identity.subject)
            except ValueError as error:
                raise ValueError("legacy identity subject must be a Telegram ID") from error
            candidates.append(
                TelegramIdentityInput(
                    telegram_id=generic_telegram_id,
                    username=self.identity.username,
                )
            )

        if telegram_user_id is not None:
            candidates.append(
                TelegramIdentityInput(
                    telegram_id=telegram_user_id,
                    username=telegram_username,
                )
            )

        if not candidates:
            return self

        canonical = candidates[0]
        conflicts = []
        for candidate in candidates[1:]:
            if candidate.telegram_id != canonical.telegram_id:
                conflicts.append("telegram_id")
            if (
                candidate.username is not None
                and canonical.username is not None
                and candidate.username != canonical.username
            ):
                conflicts.append("username")
        if conflicts:
            raise ValueError(
                "Telegram identity payloads have conflicts: "
                + ", ".join(sorted(set(conflicts)))
            )
        self.telegram_identity = TelegramIdentityInput(
            telegram_id=canonical.telegram_id,
            username=next(
                (candidate.username for candidate in candidates if candidate.username),
                None,
            ),
        )
        return self

    def resolved_telegram_identity(self) -> Optional[TelegramIdentity]:
        return (
            self.telegram_identity.to_runtime_identity()
            if self.telegram_identity
            else None
        )


class ReplyContext(BaseModel):
    role: Literal["assistant", "user"]
    message: str = Field(..., min_length=1)


class InvokeRequest(IdentityRequestMixin):
    message: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    source: Optional[str] = None
    request_id: Optional[str] = None
    thread_id: Optional[str] = None
    allow_mutations: Optional[bool] = None
    reply_context: Optional[ReplyContext] = None


class ResumeRequest(IdentityRequestMixin):
    thread_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    source: Optional[str] = None
    request_id: Optional[str] = None
    allow_mutations: Optional[bool] = None


class BulkInvokeRequest(IdentityRequestMixin):
    messages: List[str] = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    source: Optional[str] = None
    request_id: Optional[str] = None
    allow_mutations: Optional[bool] = None
    max_agent_turns: Optional[int] = None


class AgentResponse(BaseModel):
    status: Literal["completed", "interrupted", "failed"]
    thread_id: str
    response: str
    reasoning_content: Optional[str] = None
    interrupt: Optional[Dict[str, Any]] = None
    tool_results: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None


class BulkAgentResponse(BaseModel):
    results: List[AgentResponse]


class CancelRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    request_id: str = Field(..., min_length=1)


class CancelResponse(BaseModel):
    outcome: Literal[
        "cancelled",
        "mutation_in_flight",
        "already_finished",
        "not_found",
    ]
    request_id: str


class DependencyHealth(BaseModel):
    """Provider-neutral result of one detailed-health dependency probe."""

    ok: bool
    detail: str = Field(..., min_length=1)


class HealthLimits(BaseModel):
    run_deadline_seconds: float = Field(..., gt=0)
    max_agent_turns: int = Field(..., gt=0)
    llm_request_timeout_seconds: float = Field(..., gt=0)
    model_router_complex_timeout_seconds: float = Field(..., gt=0)


class DetailedHealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    provider: Literal["deepseek", "openai"]
    model: str = Field(..., min_length=1)
    checks: Dict[str, DependencyHealth]
    limits: HealthLimits
