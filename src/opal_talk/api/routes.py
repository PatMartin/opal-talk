"""REST endpoints. Every operation's `operation_id` equals the contract's `operationId`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from opal_talk import API_VERSION, SERVICE_NAME, __version__
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
from opal_talk.service.container import Services
from opal_talk.tts.base import Report

router = APIRouter()

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
def list_engines(svc: Services = Depends(services)):
    return svc.engines.list()


@router.get("/api/v1/engines/{engine_id}", tags=["engines"], operation_id="get_engine_api_v1_engines__engine_id__get",
            response_model=EngineRead)
def get_engine(engine_id: str, svc: Services = Depends(services)):
    return svc.engines.get(engine_id)


@router.patch("/api/v1/engines/{engine_id}", tags=["engines"],
              operation_id="update_engine_api_v1_engines__engine_id__patch", response_model=EngineRead)
def update_engine(engine_id: str, body: EngineUpdate, svc: Services = Depends(services)):
    return svc.engines.update(engine_id, body.changes())


@router.post("/api/v1/engines/{engine_id}/validate", tags=["engines"],
             operation_id="validate_engine_api_v1_engines__engine_id__validate_post", response_model=ValidationResult)
def validate_engine(engine_id: str, svc: Services = Depends(services)):
    return report_dict(svc.engines.validate(engine_id))


# -- voices ----------------------------------------------------------------------


@router.get("/api/v1/voices/", tags=["voices"], operation_id="list_voices_api_v1_voices__get",
            response_model=list[VoiceRead])
def list_voices(
    engine_id: str | None = Query(None, min_length=1),
    enabled: bool | None = Query(None),
    svc: Services = Depends(services),
):
    return svc.voices.list(engine_id, enabled)


@router.post("/api/v1/voices/", tags=["voices"], operation_id="create_voice_api_v1_voices__post",
             response_model=VoiceRead, status_code=201)
def create_voice(body: VoiceCreate, svc: Services = Depends(services)):
    return svc.voices.create(body)


@router.get("/api/v1/voices/{voice_id}", tags=["voices"], operation_id="get_voice_api_v1_voices__voice_id__get",
            response_model=VoiceRead)
def get_voice(voice_id: str, svc: Services = Depends(services)):
    return svc.voices.get(voice_id)


@router.patch("/api/v1/voices/{voice_id}", tags=["voices"], operation_id="update_voice_api_v1_voices__voice_id__patch",
              response_model=VoiceRead)
def update_voice(voice_id: str, body: VoiceUpdate, svc: Services = Depends(services)):
    return svc.voices.update(voice_id, body)


@router.delete("/api/v1/voices/{voice_id}", tags=["voices"],
               operation_id="delete_voice_api_v1_voices__voice_id__delete", status_code=204)
def delete_voice(voice_id: str, svc: Services = Depends(services)):
    svc.voices.delete(voice_id)
    return Response(status_code=204)


@router.post("/api/v1/voices/{voice_id}/validate", tags=["voices"],
             operation_id="validate_voice_api_v1_voices__voice_id__validate_post", response_model=ValidationResult)
def validate_voice(voice_id: str, svc: Services = Depends(services)):
    return report_dict(svc.voices.validate(voice_id))


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
def list_profiles(svc: Services = Depends(services)):
    return svc.profiles.list()


@router.post("/api/v1/profiles/", tags=["profiles"], operation_id="create_profile_api_v1_profiles__post",
             response_model=ProfileRead, status_code=201)
def create_profile(body: ProfileCreate, svc: Services = Depends(services)):
    return svc.profiles.create(body)


@router.get("/api/v1/profiles/{profile_id}", tags=["profiles"],
            operation_id="get_profile_api_v1_profiles__profile_id__get", response_model=ProfileRead)
def get_profile(profile_id: int = ProfileId, svc: Services = Depends(services)):
    return svc.profiles.get(profile_id)


@router.patch("/api/v1/profiles/{profile_id}", tags=["profiles"],
              operation_id="update_profile_api_v1_profiles__profile_id__patch", response_model=ProfileRead)
def update_profile(body: ProfileUpdate, profile_id: int = ProfileId, svc: Services = Depends(services)):
    return svc.profiles.update(profile_id, body)


@router.delete("/api/v1/profiles/{profile_id}", tags=["profiles"],
               operation_id="delete_profile_api_v1_profiles__profile_id__delete", status_code=204)
def delete_profile(profile_id: int = ProfileId, svc: Services = Depends(services)):
    svc.profiles.delete(profile_id)
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
async def stream_audio(body: GenerateStreamRequest, svc: Services = Depends(services)):
    plan = await run_in_threadpool(svc.generation.plan, body)
    blocks = svc.generation.pcm_stream(plan)
    # Produce the first block before committing to 200 so early failures become JSON errors.
    first = await run_in_threadpool(next, blocks, None)

    async def body_iter():
        if first is None:
            return
        yield first
        async for block in iterate_in_threadpool(blocks):
            yield block

    return StreamingResponse(body_iter(), media_type="application/octet-stream", headers=plan.headers("pcm"))


# -- settings --------------------------------------------------------------------


@router.get("/api/v1/settings/", tags=["settings"], operation_id="get_all_settings_api_v1_settings__get")
def get_all_settings(svc: Services = Depends(services)) -> dict[str, Any]:
    return svc.settings.all()


@router.get("/api/v1/settings/{key}", tags=["settings"], operation_id="get_setting_api_v1_settings__key__get",
            response_model=SettingRead)
def get_setting(key: str = SettingKey, svc: Services = Depends(services)):
    return svc.settings.get(key)


@router.put("/api/v1/settings/{key}", tags=["settings"], operation_id="upsert_setting_api_v1_settings__key__put",
            response_model=SettingRead)
def upsert_setting(body: SettingUpdate, key: str = SettingKey, svc: Services = Depends(services)):
    return svc.settings.upsert(key, body.value)


@router.delete("/api/v1/settings/{key}", tags=["settings"], operation_id="delete_setting_api_v1_settings__key__delete",
               status_code=204)
def delete_setting(key: str = SettingKey, svc: Services = Depends(services)):
    svc.settings.delete(key)
    return Response(status_code=204)


# -- health ----------------------------------------------------------------------


@router.get("/api/v1/health/", tags=["health"], operation_id="health_check_api_v1_health__get",
            response_model=HealthRead)
def health_check(svc: Services = Depends(services)):
    return svc.health()


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
