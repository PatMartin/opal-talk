"""FastAPI application factory for opal-talk-svc."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
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


def error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {"code": code, "message": message, "details": details or {}}


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
    async def _api_error(_request: Request, exc: ApiError):
        return JSONResponse(error_body(exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(InvalidRequest)
    async def _invalid(_request: Request, exc: InvalidRequest):
        return JSONResponse({"detail": exc.errors}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(error_body(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(error_body("internal_error", "internal server error"), status_code=500)

    app.include_router(router)

    if config.spa_dir and config.spa_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=config.spa_dir, html=True), name="ui")
    log.info("%s %s ready", SERVICE_NAME, __version__)
    return app
