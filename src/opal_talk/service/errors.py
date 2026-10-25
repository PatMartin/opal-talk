"""Service-layer errors mapped to HTTP responses by the API layer."""

from __future__ import annotations

from typing import Any

from opal_talk.tts.base import Issue


class ApiError(Exception):
    status = 500
    default_code = "error"

    def __init__(self, message: str, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.details = details or {}


class NotFound(ApiError):
    status = 404
    default_code = "not_found"


class Conflict(ApiError):
    status = 409
    default_code = "conflict"


class PayloadTooLarge(ApiError):
    status = 413
    default_code = "payload_too_large"


class GenerationFailed(ApiError):
    status = 500
    default_code = "generation_failed"


class OperationFailed(ApiError):
    status = 500
    default_code = "operation_failed"


class Unavailable(ApiError):
    status = 503
    default_code = "engine_unavailable"


class PreconditionRequired(ApiError):
    status = 428
    default_code = "precondition_required"


class PreconditionFailed(ApiError):
    status = 412
    default_code = "precondition_failed"


class InvalidRequest(Exception):
    """Semantic request validation failure, reported in the HTTPValidationError (422) shape."""

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("; ".join(e["msg"] for e in errors))
        self.errors = errors

    @classmethod
    def single(cls, loc: list[str | int], msg: str, type_: str = "value_error", input_: Any = None) -> InvalidRequest:
        return cls([{"loc": loc, "msg": msg, "type": type_, "input": input_}])

    @classmethod
    def from_issues(cls, base_loc: list[str | int], issues: list[Issue], options: dict[str, Any]) -> InvalidRequest:
        return cls(
            [
                {
                    "loc": [*base_loc, i.field] if i.field else base_loc,
                    "msg": i.message,
                    "type": i.code,
                    "input": options.get(i.field) if i.field else None,
                }
                for i in issues
            ]
        )
