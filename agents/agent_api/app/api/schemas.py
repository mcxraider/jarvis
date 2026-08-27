"""Pydantic schemas for the Jarvis FastAPI contract."""

import base64
import binascii
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import AfterValidator, BaseModel, Field, PrivateAttr, model_validator

from agents.agent_api.app.user_context.identity import TelegramIdentity


MAX_IMAGE_COUNT = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BATCHES = 20
JPEG_DATA_URL_PREFIX = "data:image/jpeg;base64,"


class ImageInput(BaseModel):
    image_url: str
    detail: Literal["auto"]
    _decoded_len: int = PrivateAttr(default=0)

    @model_validator(mode="after")
    def _validate_and_cache(self) -> "ImageInput":
        if not self.image_url.startswith(JPEG_DATA_URL_PREFIX):
            raise ValueError("image_url must be a JPEG Base64 data URL")
        try:
            decoded = base64.b64decode(
                self.image_url[len(JPEG_DATA_URL_PREFIX) :], validate=True
            )
        except (binascii.Error, ValueError) as error:
            raise ValueError("image_url must contain valid Base64") from error
        if not decoded:
            raise ValueError("image_url must not be empty")
        self._decoded_len = len(decoded)
        return self

    @property
    def decoded_bytes(self) -> int:
        return self._decoded_len


def _validate_images(images: List[ImageInput]) -> List[ImageInput]:
    if sum(image.decoded_bytes for image in images) > MAX_IMAGE_BYTES:
        raise ValueError("images must not exceed 10 MiB decoded")
    return images


ImageInputs = Annotated[
    List[ImageInput],
    Field(min_length=1, max_length=MAX_IMAGE_COUNT),
    AfterValidator(_validate_images),
]

ImageBatch = Annotated[List[ImageInput], Field(max_length=MAX_IMAGE_COUNT)]
PriorImageBatches = Annotated[
    List[ImageBatch], Field(max_length=MAX_IMAGE_BATCHES)
]


def _validate_cumulative_images(
    images: Optional[ImageInputs], prior: Optional[PriorImageBatches]
) -> None:
    all_images = [*(images or ()), *(image for batch in prior or () for image in batch)]
    if len(all_images) > MAX_IMAGE_COUNT:
        raise ValueError("image history must not exceed 10 images")
    if sum(image.decoded_bytes for image in all_images) > MAX_IMAGE_BYTES:
        raise ValueError("image history must not exceed 10 MiB decoded")


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
    images: Optional[ImageInputs] = None


class ResumeRequest(IdentityRequestMixin):
    thread_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    source: Optional[str] = None
    request_id: Optional[str] = None
    allow_mutations: Optional[bool] = None
    images: Optional[ImageInputs] = None
    prior_image_batches: Optional[PriorImageBatches] = None

    @model_validator(mode="after")
    def validate_image_history(self) -> "ResumeRequest":
        _validate_cumulative_images(self.images, self.prior_image_batches)
        return self


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
