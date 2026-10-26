"""Service-wide key/value settings. Known keys are validated; other keys are stored verbatim."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from opal_talk.persistence.store import Store
from opal_talk.service.errors import InvalidRequest, NotFound


def _number(kind: type, lo: float, hi: float) -> Callable[[str], Any]:
    def parse(raw: str) -> Any:
        try:
            value = kind(raw)
        except ValueError:
            raise ValueError(f"must be a {kind.__name__}") from None
        if not lo <= value <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return value

    return parse


def _choice(*choices: str) -> Callable[[str], str]:
    def parse(raw: str) -> str:
        if raw not in choices:
            raise ValueError(f"must be one of {list(choices)}")
        return raw

    return parse


# key -> (parser, default). Defaults apply when the key is not stored.
KNOWN_SETTINGS: dict[str, tuple[Callable[[str], Any], Any]] = {
    "max_text_chars": (_number(int, 1, 1_000_000), 20_000),
    "max_audio_bytes": (_number(int, 1 << 16, 1 << 29), 128 << 20),
    "chunk_max_chars": (_number(int, 50, 5_000), 400),
    "default_speed": (_number(float, 0.25, 4.0), 1.0),
    "default_volume": (_number(float, 0.0, 2.0), 1.0),
    "default_output_format": (_choice("wav", "pcm"), "wav"),
}


def settings_to_str(raw: dict[str, Any]) -> dict[str, str]:
    """Normalize setting values to storage strings (shared by PATCH, YAML import and restore)."""
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            out[str(key)] = "true" if value else "false"
        elif value is None:
            out[str(key)] = ""
        elif isinstance(value, dict | list):
            out[str(key)] = json.dumps(value)
        else:
            out[str(key)] = str(value)
    return out


def setting_problem(key: str, value: str) -> str | None:
    if key not in KNOWN_SETTINGS:
        return None
    try:
        KNOWN_SETTINGS[key][0](value)
    except ValueError as exc:
        return str(exc)
    return None


class SettingsService:
    def __init__(self, store: Store):
        self.store = store

    def all(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self.store.list_settings()}

    def get(self, key: str) -> dict[str, Any]:
        row = self.store.get_setting(key)
        if row is None:
            raise NotFound(f"setting '{key}' not found", details={"key": key})
        return row

    def upsert(self, key: str, value: str) -> dict[str, Any]:
        problem = setting_problem(key, value)
        if problem:
            raise InvalidRequest.single(["body", "value"], f"setting '{key}' {problem}", input_=value)
        return self.store.upsert_setting(key, value)

    def delete(self, key: str) -> None:
        self.get(key)
        self.store.delete_setting(key)

    def patch(self, changes: dict[str, Any]) -> dict[str, str]:
        """Merge-patch the whole settings document: non-null values set, `null` deletes.

        Values are normalized to strings (same rules as YAML import); known keys are
        validated. Every change is checked first, then applied in one transaction —
        all or nothing — and the full resulting map is returned. Contract 1.2.0's
        `PATCH /api/v1/settings/` sits directly on top of this.
        """
        normalized: dict[str, str | None] = {}
        errors: list[dict[str, Any]] = []
        for key, value in changes.items():
            if value is None:
                normalized[str(key)] = None
                continue
            text = settings_to_str({key: value})[key]
            problem = setting_problem(str(key), text)
            if problem:
                errors.append(
                    {"loc": ["body", str(key)], "msg": f"setting '{key}' {problem}",
                     "type": "value_error", "input": value}
                )
            else:
                normalized[str(key)] = text
        if errors:
            raise InvalidRequest(errors)
        self.store.patch_settings(normalized)
        return self.all()

    def effective(self, key: str) -> Any:
        parser, default = KNOWN_SETTINGS[key]
        row = self.store.get_setting(key)
        if row is None:
            return default
        try:
            return parser(row["value"])
        except ValueError:
            return default
