"""FastAPI application factory for opal-talk-svc."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from opal_talk import SERVICE_NAME, __version__
from opal_talk.api.routes import router
from opal_talk.config import ServiceConfig, load_config
from opal_talk.contract import load_contract
from opal_talk.service.container import Services, build_services
from opal_talk.service.errors import ApiError, InvalidRequest
from opal_talk.tts.base import TtsEngine

log = logging.getLogger(__name__)

OPAL_HEADERS = [
    "X-Opal-Audio-Format",
    "X-Opal-Sample-Rate",
    "X-Opal-Channels",
    "X-Opal-Sample-Format",
    "X-Opal-Engine-Id",
    "X-Opal-Voice-Id",
    "X-Opal-Profile-Id",
    "Content-Disposition",
]


def render_error(
    request: Request,
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    validation_errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Every error response body is produced here — the single seam for contract revisions.

    `request` is threaded through now so correlation identifiers can later be injected
    into the body without changing call sites (contract 1.2.0's OpalError envelope).
    `validation_errors` produces the request-validation shape (`{"detail": [...]}`);
    `code`/`message` are unused in that shape today but will surface in OpalError.
    """
    if validation_errors is not None:
        body: dict[str, Any] = {"detail": jsonable_encoder(validation_errors)}
    else:
        body = {"code": code, "message": message, "details": details or {}}
    return JSONResponse(body, status_code=status, headers=headers)


def create_app(
    config: ServiceConfig | None = None,
    engines: list[TtsEngine] | None = None,
    services: Services | None = None,
) -> FastAPI:
    config = config or (services.config if services else load_config())
    owns_services = services is None
    svc = services or build_services(config, engines)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owns_services:
            svc.close()

    app = FastAPI(title="OPAL Talk API", version=__version__, lifespan=lifespan)
    app.state.services = svc
    # Contract-first: publish the authoritative document rather than a generated one.
    app.openapi = load_contract  # type: ignore[method-assign]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=OPAL_HEADERS,
    )

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return render_error(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(InvalidRequest)
    async def _invalid(request: Request, exc: InvalidRequest):
        return render_error(
            request, 422, "unprocessable_entity", "request validation failed", validation_errors=exc.errors
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError):
        return render_error(
            request, 422, "unprocessable_entity", "request validation failed", validation_errors=exc.errors()
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return render_error(request, exc.status_code, code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        return render_error(request, 500, "internal_error", "internal server error")

    app.include_router(router)

    if config.spa_dir and config.spa_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=config.spa_dir, html=True), name="ui")
    log.info("%s %s ready", SERVICE_NAME, __version__)
    return app
