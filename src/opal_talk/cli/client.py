"""HTTP client used by the CLI. All CLI functionality goes through the REST API."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import httpx

from opal_talk.contract import contract_operations

DEFAULT_API_URL = "http://127.0.0.1:8001"

# operationId -> (method, path template), derived from the packaged contract so it can
# never drift from what the service publishes.
OPERATIONS: dict[str, tuple[str, str]] = contract_operations()

# Operations that mutate logical configuration and carry the mandatory If-Match
# precondition (contract 1.2.0): the client fetches the current ETag first and
# retries once on 412.
PRECONDITION_OPS = {
    "update_engine_api_v1_engines__engine_id__patch",
    "update_voice_api_v1_voices__voice_id__patch",
    "delete_voice_api_v1_voices__voice_id__delete",
    "update_profile_api_v1_profiles__profile_id__patch",
    "delete_profile_api_v1_profiles__profile_id__delete",
    "upsert_setting_api_v1_settings__key__put",
    "delete_setting_api_v1_settings__key__delete",
    "patch_talk_settings",
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
        if isinstance(body, dict):
            # OpalError envelope ({"error": {...}}); fall back to the bare v1.0 shapes.
            err = body["error"] if isinstance(body.get("error"), dict) else body
            details = err.get("details") if isinstance(err.get("details"), dict) else {}
            field_errors = details.get("errors") if isinstance(details.get("errors"), list) else err.get("detail")
            if isinstance(field_errors, list):
                parts = [f"{'.'.join(map(str, e.get('loc', [])))}: {e.get('msg')}" for e in field_errors]
                return f"HTTP {self.status} validation error: " + "; ".join(parts)
            if "code" in err:
                return f"HTTP {self.status} {err['code']}: {err.get('message', '')}"
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

    def _config_etag(self) -> str:
        return self.http.get("/api/v1/settings/").headers.get("ETag", "")

    def call(
        self,
        op: str,
        path_params: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        method, url = self._request_args(op, path_params)
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"If-Match": self._config_etag()} if op in PRECONDITION_OPS else None
        resp = self.http.request(method, url, params=clean or None, json=json, headers=headers)
        if resp.status_code == 412:  # stale revision: refresh the ETag and retry once
            resp = self.http.request(
                method, url, params=clean or None, json=json, headers={"If-Match": self._config_etag()}
            )
        self._raise_for(resp)
        return resp

    @contextmanager
    def stream(self, op: str, json: Any = None) -> Iterator[httpx.Response]:
        method, url = self._request_args(op, None)
        with self.http.stream(method, url, json=json) as resp:
            self._raise_for(resp)
            yield resp
