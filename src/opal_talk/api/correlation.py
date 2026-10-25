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


def correlation_ids(request: Request) -> CorrelationIds:
    """Return the identifiers of the in-flight request (set by CorrelationMiddleware)."""
    return getattr(request.state, _STATE_KEY)


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
        ids = CorrelationIds(
            session_id=supplied.get(SESSION_ID.lower()),
            transaction_id=supplied.get(TRANSACTION_ID.lower()) or uuid.uuid4().hex,
            request_id=supplied.get(REQUEST_ID.lower()) or uuid.uuid4().hex,
            causation_id=supplied.get(CAUSATION_ID.lower()),
        )
        scope.setdefault("state", {})[_STATE_KEY] = ids

        async def send_with_ids(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in ids.response_headers().items():
                    headers[name] = value
            await send(message)

        errors = [
            {
                "loc": ["header", name],
                "msg": "must match pattern '^[!-~]{1,128}$'",
                "type": "string_pattern_mismatch",
                "input": value,
            }
            for name in CORRELATION_HEADERS
            if (value := supplied.get(name.lower())) is not None and not _ID_PATTERN.fullmatch(value)
        ]
        if errors:
            await JSONResponse({"detail": errors}, status_code=422)(scope, receive, send_with_ids)
            return
        await self.app(scope, receive, send_with_ids)
