"""HTTP client used by the CLI. All CLI functionality goes through the REST API."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8001"

# operationId -> (method, path template). Must match opal_talk.yaml (verified by tests).
OPERATIONS: dict[str, tuple[str, str]] = {
    "list_engines_api_v1_engines__get": ("GET", "/api/v1/engines/"),
    "get_engine_api_v1_engines__engine_id__get": ("GET", "/api/v1/engines/{engine_id}"),
    "update_engine_api_v1_engines__engine_id__patch": ("PATCH", "/api/v1/engines/{engine_id}"),
    "validate_engine_api_v1_engines__engine_id__validate_post": ("POST", "/api/v1/engines/{engine_id}/validate"),
    "list_voices_api_v1_voices__get": ("GET", "/api/v1/voices/"),
    "create_voice_api_v1_voices__post": ("POST", "/api/v1/voices/"),
    "get_voice_api_v1_voices__voice_id__get": ("GET", "/api/v1/voices/{voice_id}"),
    "update_voice_api_v1_voices__voice_id__patch": ("PATCH", "/api/v1/voices/{voice_id}"),
    "delete_voice_api_v1_voices__voice_id__delete": ("DELETE", "/api/v1/voices/{voice_id}"),
    "validate_voice_api_v1_voices__voice_id__validate_post": ("POST", "/api/v1/voices/{voice_id}/validate"),
    "test_voice_api_v1_voices__voice_id__test_post": ("POST", "/api/v1/voices/{voice_id}/test"),
    "list_profiles_api_v1_profiles__get": ("GET", "/api/v1/profiles/"),
    "create_profile_api_v1_profiles__post": ("POST", "/api/v1/profiles/"),
    "get_profile_api_v1_profiles__profile_id__get": ("GET", "/api/v1/profiles/{profile_id}"),
    "update_profile_api_v1_profiles__profile_id__patch": ("PATCH", "/api/v1/profiles/{profile_id}"),
    "delete_profile_api_v1_profiles__profile_id__delete": ("DELETE", "/api/v1/profiles/{profile_id}"),
    "activate_profile_api_v1_profiles__profile_id__activate_post": ("POST", "/api/v1/profiles/{profile_id}/activate"),
    "generate_audio_api_v1_generate_post": ("POST", "/api/v1/generate"),
    "stream_audio_api_v1_generate_stream_post": ("POST", "/api/v1/generate/stream"),
    "get_all_settings_api_v1_settings__get": ("GET", "/api/v1/settings/"),
    "get_setting_api_v1_settings__key__get": ("GET", "/api/v1/settings/{key}"),
    "upsert_setting_api_v1_settings__key__put": ("PUT", "/api/v1/settings/{key}"),
    "delete_setting_api_v1_settings__key__delete": ("DELETE", "/api/v1/settings/{key}"),
    "health_check_api_v1_health__get": ("GET", "/api/v1/health/"),
    "list_backups_api_v1_backup_list_get": ("GET", "/api/v1/backup/list"),
    "create_backup_api_v1_backup_create_post": ("POST", "/api/v1/backup/create"),
    "download_backup_api_v1_backup_download__filename__get": ("GET", "/api/v1/backup/download/{filename}"),
    "restore_backup_api_v1_backup_restore__filename__post": ("POST", "/api/v1/backup/restore/{filename}"),
    "delete_backup_api_v1_backup__filename__delete": ("DELETE", "/api/v1/backup/{filename}"),
    "export_config_api_v1_backup_export_get": ("GET", "/api/v1/backup/export"),
    "import_config_api_v1_backup_import_post": ("POST", "/api/v1/backup/import"),
    "validate_config_api_v1_backup_validate_get": ("GET", "/api/v1/backup/validate"),
    "root__get": ("GET", "/"),
    "healthz_healthz_get": ("GET", "/healthz"),
}


def api_url_from_env() -> str:
    return os.environ.get("OPAL_TALK_API_URL", DEFAULT_API_URL)


class ApiCallError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(self.describe())

    def describe(self) -> str:
        body = self.body
        if isinstance(body, dict) and "code" in body:
            return f"HTTP {self.status} {body['code']}: {body.get('message', '')}"
        if isinstance(body, dict) and isinstance(body.get("detail"), list):
            parts = [f"{'.'.join(map(str, d.get('loc', [])))}: {d.get('msg')}" for d in body["detail"]]
            return f"HTTP {self.status} validation error: " + "; ".join(parts)
        return f"HTTP {self.status}: {body}"


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 120.0, http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    @staticmethod
    def _request_args(op: str, path_params: dict[str, Any] | None) -> tuple[str, str]:
        method, template = OPERATIONS[op]
        params = {k: quote(str(v), safe="") for k, v in (path_params or {}).items()}
        return method, template.format(**params)

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            resp.read()
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise ApiCallError(resp.status_code, body)

    def call(
        self,
        op: str,
        path_params: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        method, url = self._request_args(op, path_params)
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = self.http.request(method, url, params=clean or None, json=json)
        self._raise_for(resp)
        return resp

    @contextmanager
    def stream(self, op: str, json: Any = None) -> Iterator[httpx.Response]:
        method, url = self._request_args(op, None)
        with self.http.stream(method, url, json=json) as resp:
            self._raise_for(resp)
            yield resp
