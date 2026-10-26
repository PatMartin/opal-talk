"""REST endpoints. Every operation's `operation_id` equals the contract's `operationId`."""

from __future__ import annotations

import logging
import threading
from typing import Any

import anyio
from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from opal_talk import API_VERSION, SERVICE_NAME, __version__
from opal_talk.api.correlation import correlation_ids
from opal_talk.schemas import (
    BackupCreateResponse,
    BackupRead,
    ConfigExport,
    ConfigImportResult,
    EngineRead,
    EngineUpdate,
    GenerateRequest,
    GenerateStreamRequest,
    HealthRead,
    LivenessRead,
    OperationResult,
    ProfileCreate,
    ProfileRead,
    ProfileUpdate,
    ServiceInfo,
    SettingRead,
    SettingUpdate,
    ValidationResult,
    VoiceCreate,
    VoiceRead,
    VoiceTestRequest,
    VoiceUpdate,
)
from opal_talk.service.concurrency import config_etag, mutate_with_revision
from opal_talk.service.config_candidate import Candidate
from opal_talk.service.container import Services
from opal_talk.tts.base import Report

router = APIRouter()

log = logging.getLogger(__name__)


def _correlation_context(request: Request) -> str:
    """Flatten the request's correlation identifiers for log records."""
    ids = correlation_ids(request)
    if ids is None:
        return "request_id=unknown"
    parts = [f"request_id={ids.request_id}", f"transaction_id={ids.transaction_id}"]
    if ids.session_id:
        parts.append(f"session_id={ids.session_id}")
    if ids.causation_id:
        parts.append(f"causation_id={ids.causation_id}")
    return " ".join(parts)


class _PcmBodyIterator:
    """Async iterator feeding the synchronous PCM generator of one stream request.

    `close()` — serialized with in-flight reads — is the single release path for the
    generator's runtime reference and inference permit. `__del__` schedules it as the
    last resort for a response that is abandoned without unwinding its call path.
    """

    def __init__(self, blocks, ctx: str, on_close=None):
        self._blocks = blocks
        self._ctx = ctx
        self._on_close = on_close
        self._lock = threading.Lock()
        self._first: bytes | None = None
        self._first_pending = False
        self._completed = False
        self._failed = False
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._first_pending:
            self._first_pending = False
            if self._first is not None:
                return self._first
        try:
            block = await run_in_threadpool(self.next_block)
        except Exception:
            # A synthesis failure after 200: the contract terminates the stream
            # rather than injecting an error object into the byte stream.
            self._failed = True
            log.warning("audio stream terminated by synthesis failure (%s)", self._ctx, exc_info=True)
            raise
        if block is None:
            self._completed = True
            log.debug("audio stream completed (%s)", self._ctx)
            raise StopAsyncIteration
        return block

    def prime(self, first: bytes | None) -> None:
        """Seed the block already produced while deciding whether 200 is commitable."""
        self._first = first
        self._first_pending = True

    def next_block(self) -> bytes | None:
        with self._lock:
            if self._closed:
                return None
            return next(self._blocks, None)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._blocks.close()
            except Exception:  # noqa: BLE001 - teardown failure must not mask the outcome
                log.exception("failed to release audio stream resources (%s)", self._ctx)
            try:
                # The plan-bound runtime: released even when blocks.close() ran no
                # generator body (never started) or raised.
                if self._on_close is not None:
                    self._on_close()
            except Exception:  # noqa: BLE001
                log.exception("failed to release the bound generation (%s)", self._ctx)
        if not self._completed and not self._failed:
            log.info("audio stream closed before completion (%s)", self._ctx)

    def __del__(self):
        # Off the caller's thread: teardown may have to wait out an in-flight engine call.
        try:
            if not self._closed:
                threading.Thread(target=self.close, name="opal-stream-close", daemon=True).start()
        except Exception:  # noqa: BLE001
            pass


class _PcmStreamResponse(StreamingResponse):
    """StreamingResponse that releases the PCM generator when the response ends."""

    def __init__(self, stream: _PcmBodyIterator, **kwargs):
        super().__init__(stream, **kwargs)
        self._stream = stream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Every exit — completed, failed, disconnected or externally cancelled —
            # unwinds through here, so the runtime reference and inference permit are
            # released deterministically instead of relying on garbage collection.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self._stream.close)

ProfileId = Path(ge=1, description="Numeric profile identifier.")
BackupFilename = Path(min_length=1, max_length=255, pattern=r"^[^/\\]+$")
SettingKey = Path(min_length=1)


def services(request: Request) -> Services:
    return request.app.state.services


def report_dict(report: Report) -> dict[str, Any]:
    def msg(i):
        return {"code": i.code, "message": i.message, "field": i.field, "details": i.details}

    return {
        "valid": report.valid,
        "errors": [msg(i) for i in report.errors],
        "warnings": [msg(i) for i in report.warnings],
        "details": report.details,
    }


# -- engines ---------------------------------------------------------------------


@router.get("/api/v1/engines/", tags=["engines"], operation_id="list_engines_api_v1_engines__get",
            response_model=list[EngineRead])
def list_engines(response: Response, svc: Services = Depends(services)):
    response.headers["ETag"] = config_etag(svc.store)
    return svc.engines.list()


@router.get("/api/v1/engines/{engine_id}", tags=["engines"], operation_id="get_engine_api_v1_engines__engine_id__get",
            response_model=EngineRead)
def get_engine(engine_id: str, response: Response, svc: Services = Depends(services)):
    response.headers["ETag"] = config_etag(svc.store)
    return svc.engines.get(engine_id)


@router.patch("/api/v1/engines/{engine_id}", tags=["engines"],
              operation_id="update_engine_api_v1_engines__engine_id__patch", response_model=EngineRead)
def update_engine(engine_id: str, body: EngineUpdate, if_match: str | None = Header(None),
                  svc: Services = Depends(services)):
    return mutate_with_revision(svc.store, if_match, lambda: svc.engines.update(engine_id, body.changes()))


@router.post("/api/v1/engines/{engine_id}/validate", tags=["engines"],
             operation_id="validate_engine_api_v1_engines__engine_id__validate_post", response_model=ValidationResult)
def validate_engine(engine_id: str, svc: Services = Depends(services)):
    return report_dict(svc.engines.validate(engine_id))


# -- voices ----------------------------------------------------------------------


@router.get("/api/v1/voices/", tags=["voices"], operation_id="list_voices_api_v1_voices__get",
            response_model=list[VoiceRead])
def list_voices(
    response: Response,
    engine_id: str | None = Query(None, min_length=1),
    enabled: bool | None = Query(None),
    svc: Services = Depends(services),
):
    response.headers["ETag"] = config_etag(svc.store)
    return svc.voices.list(engine_id, enabled)


@router.post("/api/v1/voices/", tags=["voices"], operation_id="create_voice_api_v1_voices__post",
             response_model=VoiceRead, status_code=201)
def create_voice(body: VoiceCreate, response: Response, svc: Services = Depends(services)):
    voice = svc.voices.create(body)
    response.headers["Location"] = f"/api/v1/voices/{voice['voice_id']}"
    return voice


@router.get("/api/v1/voices/{voice_id}", tags=["voices"], operation_id="get_voice_api_v1_voices__voice_id__get",
            response_model=VoiceRead)
def get_voice(voice_id: str, response: Response, svc: Services = Depends(services)):
    response.headers["ETag"] = config_etag(svc.store)
    return svc.voices.get(voice_id)


@router.patch("/api/v1/voices/{voice_id}", tags=["voices"], operation_id="update_voice_api_v1_voices__voice_id__patch",
              response_model=VoiceRead)
def update_voice(voice_id: str, body: VoiceUpdate, if_match: str | None = Header(None),
                svc: Services = Depends(services)):
    return mutate_with_revision(svc.store, if_match, lambda: svc.voices.update(voice_id, body))


@router.delete("/api/v1/voices/{voice_id}", tags=["voices"],
               operation_id="delete_voice_api_v1_voices__voice_id__delete", status_code=204)
def delete_voice(voice_id: str, if_match: str | None = Header(None), svc: Services = Depends(services)):
    mutate_with_revision(svc.store, if_match, lambda: svc.voices.delete(voice_id))
    return Response(status_code=204)


@router.post("/api/v1/voices/{voice_id}/validate", tags=["voices"],
             operation_id="validate_voice_api_v1_voices__voice_id__validate_post", response_model=ValidationResult)
def validate_voice(voice_id: str, svc: Services = Depends(services)):
    return report_dict(svc.voices.validate(voice_id))


# Deliberate asymmetry: CRUD handlers are sync `def` (FastAPI runs them in the threadpool
# automatically); the three audio handlers are `async` because they must control *when* work
# is dispatched — generate/voice-test produce the first block before 200 is committed, and
# stream keeps a generator alive for the response body.
@router.post("/api/v1/voices/{voice_id}/test", tags=["voices"],
             operation_id="test_voice_api_v1_voices__voice_id__test_post", response_class=Response)
async def test_voice(voice_id: str, body: VoiceTestRequest | None = Body(None), svc: Services = Depends(services)):
    plan, data, media_type = await run_in_threadpool(
        svc.generation.test_voice, voice_id, body or VoiceTestRequest()
    )
    return Response(data, media_type=media_type, headers=plan.headers())


# -- profiles --------------------------------------------------------------------


@router.get("/api/v1/profiles/", tags=["profiles"], operation_id="list_profiles_api_v1_profiles__get",
            response_model=list[ProfileRead])
def list_profiles(response: Response, svc: Services = Depends(services)):
    response.headers["ETag"] = config_etag(svc.store)
    return svc.profiles.list()


@router.post("/api/v1/profiles/", tags=["profiles"], operation_id="create_profile_api_v1_profiles__post",
             response_model=ProfileRead, status_code=201)
def create_profile(body: ProfileCreate, response: Response, svc: Services = Depends(services)):
    profile = svc.profiles.create(body)
    response.headers["Location"] = f"/api/v1/profiles/{profile['id']}"
    return profile


@router.get("/api/v1/profiles/{profile_id}", tags=["profiles"],
            operation_id="get_profile_api_v1_profiles__profile_id__get", response_model=ProfileRead)
def get_profile(response: Response, profile_id: int = ProfileId, svc: Services = Depends(services)):
    response.headers["ETag"] = config_etag(svc.store)
    return svc.profiles.get(profile_id)


@router.patch("/api/v1/profiles/{profile_id}", tags=["profiles"],
              operation_id="update_profile_api_v1_profiles__profile_id__patch", response_model=ProfileRead)
def update_profile(body: ProfileUpdate, profile_id: int = ProfileId, if_match: str | None = Header(None),
                   svc: Services = Depends(services)):
    return mutate_with_revision(svc.store, if_match, lambda: svc.profiles.update(profile_id, body))


@router.delete("/api/v1/profiles/{profile_id}", tags=["profiles"],
               operation_id="delete_profile_api_v1_profiles__profile_id__delete", status_code=204)
def delete_profile(profile_id: int = ProfileId, if_match: str | None = Header(None), svc: Services = Depends(services)):
    mutate_with_revision(svc.store, if_match, lambda: svc.profiles.delete(profile_id))
    return Response(status_code=204)


@router.post("/api/v1/profiles/{profile_id}/activate", tags=["profiles"],
             operation_id="activate_profile_api_v1_profiles__profile_id__activate_post", response_model=ProfileRead)
def activate_profile(profile_id: int = ProfileId, svc: Services = Depends(services)):
    return svc.profiles.activate(profile_id)


# -- generate --------------------------------------------------------------------


@router.post("/api/v1/generate", tags=["generate"], operation_id="generate_audio_api_v1_generate_post",
             response_class=Response)
async def generate_audio(body: GenerateRequest, svc: Services = Depends(services)):
    plan = await run_in_threadpool(svc.generation.plan, body)
    data, media_type = await run_in_threadpool(svc.generation.render, plan)
    return Response(data, media_type=media_type, headers=plan.headers())


@router.post("/api/v1/generate/stream", tags=["generate"], operation_id="stream_audio_api_v1_generate_stream_post",
             response_class=StreamingResponse)
async def stream_audio(request: Request, body: GenerateStreamRequest, svc: Services = Depends(services)):
    plan = await run_in_threadpool(svc.generation.plan, body)
    stream = _PcmBodyIterator(
        svc.generation.pcm_stream(plan), _correlation_context(request), on_close=plan.release
    )
    try:
        # Produce the first block before committing to 200 so early failures become JSON errors.
        first = await run_in_threadpool(stream.next_block)
    except BaseException:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(stream.close)
        raise
    stream.prime(first)
    return _PcmStreamResponse(stream, media_type="application/octet-stream", headers=plan.headers("pcm"))


# -- settings --------------------------------------------------------------------


@router.get("/api/v1/settings/", tags=["settings"], operation_id="get_all_settings_api_v1_settings__get")
def get_all_settings(response: Response, svc: Services = Depends(services)) -> dict[str, Any]:
    response.headers["ETag"] = config_etag(svc.store)
    return svc.settings.all()


@router.patch("/api/v1/settings/", tags=["settings"], operation_id="patch_talk_settings")
def patch_settings(body: dict[str, Any] = Body(min_length=1), if_match: str | None = Header(None),
                   svc: Services = Depends(services)) -> dict[str, Any]:
    return mutate_with_revision(svc.store, if_match, lambda: svc.settings.patch(body))


@router.get("/api/v1/settings/{key}", tags=["settings"], operation_id="get_setting_api_v1_settings__key__get",
            response_model=SettingRead)
def get_setting(response: Response, key: str = SettingKey, svc: Services = Depends(services)):
    response.headers["ETag"] = config_etag(svc.store)
    return svc.settings.get(key)


@router.put("/api/v1/settings/{key}", tags=["settings"], operation_id="upsert_setting_api_v1_settings__key__put",
            response_model=SettingRead)
def upsert_setting(body: SettingUpdate, key: str = SettingKey, if_match: str | None = Header(None),
                   svc: Services = Depends(services)):
    return mutate_with_revision(svc.store, if_match, lambda: svc.settings.upsert(key, body.value))


@router.delete("/api/v1/settings/{key}", tags=["settings"], operation_id="delete_setting_api_v1_settings__key__delete",
               status_code=204)
def delete_setting(key: str = SettingKey, if_match: str | None = Header(None), svc: Services = Depends(services)):
    mutate_with_revision(svc.store, if_match, lambda: svc.settings.delete(key))
    return Response(status_code=204)


# -- health ----------------------------------------------------------------------


@router.get("/api/v1/health/", tags=["health"], operation_id="health_check_api_v1_health__get",
            response_model=HealthRead)
def health_check(svc: Services = Depends(services)):
    return svc.health()


@router.get("/api/v1/health/live", tags=["health"], operation_id="get_talk_health_live")
def health_live() -> dict[str, Any]:
    return {"status": "ok", "ready": True}


@router.get("/api/v1/health/ready", tags=["health"], operation_id="get_talk_health_ready")
def health_ready(svc: Services = Depends(services)):
    health = svc.health()
    usable = health["status"] != "error"
    body = {"status": health["status"] if usable else "unavailable", "ready": usable, "details": health["details"]}
    return JSONResponse(body, status_code=200 if usable else 503)


# -- backup ----------------------------------------------------------------------


@router.get("/api/v1/backup/list", tags=["backup"], operation_id="list_backups_api_v1_backup_list_get",
            response_model=list[BackupRead])
def list_backups(svc: Services = Depends(services)):
    return svc.config_ops.list_backups()


@router.post("/api/v1/backup/create", tags=["backup"], operation_id="create_backup_api_v1_backup_create_post",
             response_model=BackupCreateResponse)
def create_backup(svc: Services = Depends(services)):
    return svc.config_ops.create_backup()


@router.get("/api/v1/backup/download/{filename}", tags=["backup"],
            operation_id="download_backup_api_v1_backup_download__filename__get", response_class=FileResponse)
def download_backup(filename: str = BackupFilename, svc: Services = Depends(services)):
    path = svc.config_ops.backup_path(filename)
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


@router.post("/api/v1/backup/restore/{filename}", tags=["backup"],
             operation_id="restore_backup_api_v1_backup_restore__filename__post", response_model=OperationResult)
def restore_backup(filename: str = BackupFilename, svc: Services = Depends(services)):
    return svc.config_ops.restore_backup(filename)


@router.get("/api/v1/backup/export", tags=["backup"], operation_id="export_config_api_v1_backup_export_get",
            response_model=ConfigExport)
def export_config(svc: Services = Depends(services)):
    return svc.config_ops.export()


@router.post("/api/v1/backup/import", tags=["backup"], operation_id="import_config_api_v1_backup_import_post",
             response_model=ConfigImportResult)
def import_config(svc: Services = Depends(services)):
    result = svc.config_ops.import_config()
    return {**result, "validation": report_dict(result["validation"])}


@router.get("/api/v1/backup/validate", tags=["backup"], operation_id="validate_config_api_v1_backup_validate_get",
            response_model=ValidationResult)
def validate_config(svc: Services = Depends(services)):
    return report_dict(svc.config_ops.validate_current())


@router.post("/api/v1/backup/validate", tags=["backup"], operation_id="validate_talk_backup_payload",
             response_model=ValidationResult)
def validate_backup_payload(body: ConfigExport, svc: Services = Depends(services)):
    """Validate a supplied configuration payload against host resources. No writes."""
    dump = body.model_dump(mode="json")
    cand = Candidate(
        engines={e["engine_id"]: {"enabled": e["enabled"], "options": e["options"]} for e in dump["engines"]},
        voices=dump["voices"],
        profiles=dump["profiles"],
        settings={k: str(v) for k, v in dump["settings"].items()},
    )
    return report_dict(svc.config_ops.validate_candidate(cand, resources_are_errors=True)[0])


# Registered after /backup/list etc. so the literal paths win for GET/POST.
@router.delete("/api/v1/backup/{filename}", tags=["backup"],
               operation_id="delete_backup_api_v1_backup__filename__delete", status_code=204)
def delete_backup(filename: str = BackupFilename, svc: Services = Depends(services)):
    svc.config_ops.delete_backup(filename)
    return Response(status_code=204)


# -- service identity ---------------------------------------------------------------


@router.get("/", operation_id="root__get", response_model=ServiceInfo)
def root():
    return {
        "name": SERVICE_NAME,
        "version": __version__,
        "api_version": API_VERSION,
        "description": "Implementation-neutral OPAL text-to-speech service",
    }


@router.get("/healthz", operation_id="healthz_healthz_get", response_model=LivenessRead)
def healthz():
    return {"status": "ok"}
