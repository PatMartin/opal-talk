"""OPAL suite correlation identifiers (X-Opal-* headers) as pure-ASGI middleware.

Implements the contract-1.2.0 context convention ahead of the contract swap: every
request may carry four optional correlation headers, and every response — JSON,
binary, streamed or error — echoes them. Supplied values must be opaque printable
ASCII tokens of 1..128 characters; invalid values are rejected with 422. Missing
transaction and request IDs are generated; session and causation IDs propagate
only when supplied. Handlers read them via `correlation_ids(request)`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SESSION_ID = "X-Opal-Session-Id"
TRANSACTION_ID = "X-Opal-Transaction-Id"
REQUEST_ID = "X-Opal-Request-Id"
CAUSATION_ID = "X-Opal-Causation-Id"

#: All four correlation header names; also re-exported for CORS `expose_headers`.
CORRELATION_HEADERS = (SESSION_ID, TRANSACTION_ID, REQUEST_ID, CAUSATION_ID)

_STATE_KEY = "opal_ids"
_ID_PATTERN = re.compile(r"^[!-~]{1,128}$")


@dataclass(frozen=True)
class CorrelationIds:
    """Resolved correlation identifiers for one inbound request."""

    session_id: str | None
    transaction_id: str
    request_id: str
    causation_id: str | None

    def response_headers(self) -> dict[str, str]:
        headers = {TRANSACTION_ID: self.transaction_id, REQUEST_ID: self.request_id}
        if self.session_id is not None:
            headers[SESSION_ID] = self.session_id
        if self.causation_id is not None:
            headers[CAUSATION_ID] = self.causation_id
        return headers


def correlation_ids(request: Request) -> CorrelationIds | None:
    """Return the identifiers of the in-flight request (set by CorrelationMiddleware)."""
    return getattr(request.state, _STATE_KEY, None)


def opal_error_body(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    ids: CorrelationIds | None = None,
) -> dict[str, Any]:
    """Serialize an error in the shared `OpalError` envelope (contract 1.2.0).

    `request_id`/`transaction_id` are required by the schema; they come from the
    request's CorrelationIds when available and are generated otherwise.
    """
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": ids.request_id if ids else uuid.uuid4().hex,
        "transaction_id": ids.transaction_id if ids else uuid.uuid4().hex,
        "details": details or {},
    }
    if ids and ids.session_id is not None:
        error["session_id"] = ids.session_id
    if ids and ids.causation_id is not None:
        error["causation_id"] = ids.causation_id
    return {"error": error}


class CorrelationMiddleware:
    """Validate, assign and echo the OPAL correlation headers on every HTTP exchange.

    Pure ASGI (not BaseHTTPMiddleware) so streaming and binary responses are not
    buffered: identifiers are stamped onto `http.response.start` before any body.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in scope["headers"]}
        valid = {
            name: value
            for name in CORRELATION_HEADERS
            if (value := supplied.get(name.lower())) is not None and _ID_PATTERN.fullmatch(value)
        }
        errors = [
            {
                "loc": ["header", name],
                "msg": "must match pattern '^[!-~]{1,128}$'",
                "type": "string_pattern_mismatch",
                "input": supplied[name.lower()],
            }
            for name in CORRELATION_HEADERS
            if name.lower() in supplied and name not in valid
        ]
        # Invalid client values are treated as absent: rejected with 422 and never
        # echoed back — transaction/request IDs fall back to generated ones.
        ids = CorrelationIds(
            session_id=valid.get(SESSION_ID),
            transaction_id=valid.get(TRANSACTION_ID) or uuid.uuid4().hex,
            request_id=valid.get(REQUEST_ID) or uuid.uuid4().hex,
            causation_id=valid.get(CAUSATION_ID),
        )
        scope.setdefault("state", {})[_STATE_KEY] = ids

        async def send_with_ids(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in ids.response_headers().items():
                    headers[name] = value
            await send(message)

        if errors:
            body = opal_error_body(
                "unprocessable_entity", "request validation failed", {"errors": errors}, ids=ids
            )
            await JSONResponse(body, status_code=422)(scope, receive, send_with_ids)
            return
        await self.app(scope, receive, send_with_ids)
