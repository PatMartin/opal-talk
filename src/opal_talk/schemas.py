"""Pydantic DTOs mirroring `components.schemas` in opal_talk.yaml (the contract is authoritative)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

AudioFormat = Literal["wav", "pcm"]
VOICE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
SampleRate = Annotated[int, Field(ge=8000, le=192000)]
Speed = Annotated[float, Field(ge=0.25, le=4.0)]
Volume = Annotated[float, Field(ge=0.0, le=2.0)]
EngineOptions = dict[str, Any]

_T = TypeVar("_T")


def _reject_null(value: Any) -> Any:
    if value is None:
        raise ValueError("may not be null")
    return value


# A PATCH body field: may be omitted entirely, but may not be sent as JSON null.
NonNull = Annotated[_T | None, BeforeValidator(_reject_null)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Patch(_Request):
    """PATCH bodies: `minProperties: 1`; non-nullable fields may be omitted but not null."""

    @model_validator(mode="after")
    def _non_empty(self):
        if not self.model_fields_set:
            raise ValueError("at least one property must be supplied")
        return self

    def changes(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.model_fields_set}


# -- engines -------------------------------------------------------------------


class EngineRead(BaseModel):
    engine_id: str
    name: str
    description: str | None = None
    enabled: bool
    available: bool
    options: EngineOptions
    metadata: dict[str, Any]


class EngineUpdate(_Patch):
    enabled: NonNull[bool] = None
    options: NonNull[EngineOptions] = None


# -- voices --------------------------------------------------------------------


class VoiceCreate(_Request):
    voice_id: str = Field(min_length=1, max_length=200, pattern=VOICE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    engine_id: str = Field(min_length=1)
    engine_voice_ref: str = Field(min_length=1, max_length=2048)
    language: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    engine_options: EngineOptions = Field(default_factory=dict)


class VoiceUpdate(_Patch):
    name: NonNull[str] = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    engine_id: NonNull[str] = Field(default=None, min_length=1)
    engine_voice_ref: NonNull[str] = Field(default=None, min_length=1, max_length=2048)
    language: str | None = Field(default=None, max_length=64)
    enabled: NonNull[bool] = None
    engine_options: NonNull[EngineOptions] = None


class VoiceRead(BaseModel):
    voice_id: str
    name: str
    description: str | None = None
    engine_id: str
    engine_voice_ref: str
    language: str | None = None
    enabled: bool
    native_sample_rate: int | None = None
    engine_options: EngineOptions
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class VoiceTestRequest(_Request):
    text: str = Field(default="This is a test of the selected OPAL Talk voice.", min_length=1)
    speed: Speed = 1.0
    volume: Volume = 1.0
    output_format: AudioFormat = "wav"
    sample_rate: SampleRate | None = None
    engine_options: EngineOptions = Field(default_factory=dict)


# -- profiles ------------------------------------------------------------------


class ProfileCreate(_Request):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool = False
    voice_id: str = Field(min_length=1)
    speed: Speed = 1.0
    volume: Volume = 1.0
    output_format: AudioFormat = "wav"
    sample_rate: SampleRate | None = None
    engine_options: EngineOptions = Field(default_factory=dict)


class ProfileUpdate(_Patch):
    name: NonNull[str] = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    is_active: NonNull[bool] = None
    voice_id: NonNull[str] = Field(default=None, min_length=1)
    speed: NonNull[Speed] = None
    volume: NonNull[Volume] = None
    output_format: NonNull[AudioFormat] = None
    sample_rate: SampleRate | None = None
    engine_options: NonNull[EngineOptions] = None


class ProfileRead(BaseModel):
    id: int
    name: str
    description: str | None = None
    is_active: bool
    voice_id: str
    speed: float
    volume: float
    output_format: AudioFormat
    sample_rate: int | None = None
    engine_options: EngineOptions
    created_at: str
    updated_at: str


# -- generation ----------------------------------------------------------------


class GenerateStreamRequest(_Request):
    text: str = Field(min_length=1)
    profile_id: Annotated[int, Field(ge=1)] | None = None
    voice_id: Annotated[str, Field(min_length=1)] | None = None
    engine_id: Annotated[str, Field(min_length=1)] | None = None
    speed: Speed | None = None
    volume: Volume | None = None
    sample_rate: SampleRate | None = None
    engine_options: EngineOptions = Field(default_factory=dict)


class GenerateRequest(GenerateStreamRequest):
    output_format: AudioFormat | None = None


# -- settings / health / backup -------------------------------------------------


class SettingRead(BaseModel):
    id: int
    key: str
    value: str
    updated_at: str


class SettingUpdate(_Request):
    value: str


class ValidationMessage(BaseModel):
    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationMessage]
    warnings: list[ValidationMessage]
    details: dict[str, Any]


class EngineHealthRead(BaseModel):
    engine_id: str
    enabled: bool
    available: bool
    message: str | None = None


class HealthRead(BaseModel):
    status: Literal["ok", "degraded", "error"]
    version: str
    active_profile_id: int | None = None
    engines: list[EngineHealthRead]
    details: dict[str, Any] = Field(default_factory=dict)


class BackupRead(BaseModel):
    filename: str
    created_at: str
    size_bytes: int | None = None


class BackupCreateResponse(BaseModel):
    filename: str
    message: str


class ConfigExport(BaseModel):
    version: str
    exported_at: str
    engines: list[EngineRead]
    voices: list[VoiceRead]
    profiles: list[ProfileRead]
    settings: dict[str, Any]


class ConfigImportResult(BaseModel):
    success: bool
    validation: ValidationResult
    message: str | None = None


class OperationResult(BaseModel):
    success: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ServiceInfo(BaseModel):
    name: str
    version: str
    api_version: str
    description: str


class LivenessRead(BaseModel):
    status: Literal["ok"] = "ok"
