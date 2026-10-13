"""Engines, voice registrations and profiles."""

from __future__ import annotations

import logging
from typing import Any

from opal_talk.persistence.store import Store, now_iso
from opal_talk.schemas import ProfileCreate, ProfileUpdate, VoiceCreate, VoiceUpdate
from opal_talk.service.errors import Conflict, InvalidRequest, NotFound
from opal_talk.tts.base import EngineUnavailableError, Issue, Report, TtsEngine, VoiceResolutionError
from opal_talk.tts.registry import EngineRegistry

log = logging.getLogger(__name__)


def merge_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Shallow JSON merge-patch: keys in `patch` replace, `null` removes."""
    out = dict(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


class EngineService:
    def __init__(self, store: Store, registry: EngineRegistry):
        self.store = store
        self.registry = registry

    def bootstrap(self, seed: dict[str, dict[str, Any]]) -> None:
        """Create persisted state for newly discovered engines and push options into adapters."""
        for engine in self.registry.all():
            state = self.store.get_engine_state(engine.engine_id)
            if state is None:
                cfg = seed.get(engine.engine_id, {})
                options = {**engine.default_options(), **(cfg.get("options") or {})}
                self.store.upsert_engine_state(engine.engine_id, bool(cfg.get("enabled", True)), options)
                state = self.store.get_engine_state(engine.engine_id)
            self.apply(engine, state["options"])

    @staticmethod
    def apply(engine: TtsEngine, options: dict[str, Any]) -> None:
        issues = engine.validate_options(options, "engine")
        if issues:
            log.warning("engine %s has invalid options: %s", engine.engine_id, [i.message for i in issues])
        engine.configure(options)

    def engine(self, engine_id: str) -> TtsEngine:
        engine = self.registry.get(engine_id)
        if engine is None:
            raise NotFound(f"engine '{engine_id}' not found", "engine_not_found", {"engine_id": engine_id})
        return engine

    def state(self, engine_id: str) -> dict[str, Any]:
        state = self.store.get_engine_state(engine_id)
        return state or {"engine_id": engine_id, "enabled": True, "options": {}}

    def read(self, engine: TtsEngine) -> dict[str, Any]:
        state = self.state(engine.engine_id)
        available, _ = engine.availability()
        return {
            "engine_id": engine.engine_id,
            "name": engine.name,
            "description": engine.description,
            "enabled": state["enabled"],
            "available": available,
            "options": state["options"],
            "metadata": engine.metadata(),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.read(e) for e in self.registry.all()]

    def get(self, engine_id: str) -> dict[str, Any]:
        return self.read(self.engine(engine_id))

    def update(self, engine_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        engine = self.engine(engine_id)
        state = self.state(engine_id)
        options = state["options"]
        if "options" in changes:
            options = merge_patch(options, changes["options"])
            issues = engine.validate_options(options, "engine")
            if issues:
                raise InvalidRequest.from_issues(["body", "options"], issues, options)
        enabled = changes.get("enabled", state["enabled"])
        self.store.upsert_engine_state(engine_id, enabled, options)
        engine.configure(options)
        return self.read(engine)

    def validate(self, engine_id: str) -> Report:
        engine = self.engine(engine_id)
        report = engine.validate()
        if not self.state(engine_id)["enabled"]:
            report.warnings.append(Issue("engine_disabled", f"engine '{engine_id}' is administratively disabled"))
        return report

    def usability_problem(self, engine_id: str) -> tuple[str, str] | None:
        """Return (code, message) if generation cannot be routed to this engine."""
        engine = self.registry.get(engine_id)
        if engine is None:
            return "engine_not_found", f"engine '{engine_id}' is not installed"
        if not self.state(engine_id)["enabled"]:
            return "engine_disabled", f"engine '{engine_id}' is disabled"
        available, message = engine.availability()
        if not available:
            return "engine_unavailable", message or f"engine '{engine_id}' is unavailable"
        return None


class VoiceService:
    def __init__(self, store: Store, engines: EngineService):
        self.store = store
        self.engines = engines

    def get(self, voice_id: str) -> dict[str, Any]:
        voice = self.store.get_voice(voice_id)
        if voice is None:
            raise NotFound(f"voice '{voice_id}' not found", "voice_not_found", {"voice_id": voice_id})
        return voice

    def list(self, engine_id: str | None, enabled: bool | None) -> list[dict[str, Any]]:
        return self.store.list_voices(engine_id, enabled)

    def _describe(self, engine: TtsEngine, ref: str, options: dict[str, Any]) -> dict[str, Any]:
        """Validate options and resolve the host resource (when the engine can currently do so)."""
        issues = engine.validate_options(options, "voice")
        if issues:
            raise InvalidRequest.from_issues(["body", "engine_options"], issues, options)
        if not engine.availability()[0]:
            return {"native_sample_rate": None, "metadata": {}, "language": None}
        try:
            desc = engine.describe_voice(ref, options)
        except VoiceResolutionError as exc:
            raise InvalidRequest.single(["body", "engine_voice_ref"], str(exc), exc.code, ref) from exc
        except EngineUnavailableError:
            return {"native_sample_rate": None, "metadata": {}, "language": None}
        return {"native_sample_rate": desc.native_sample_rate, "metadata": desc.metadata, "language": desc.language}

    def create(self, body: VoiceCreate) -> dict[str, Any]:
        engine = self.engines.engine(body.engine_id)
        if self.store.get_voice(body.voice_id) is not None:
            raise Conflict(f"voice '{body.voice_id}' is already registered", "voice_exists", {"voice_id": body.voice_id})
        desc = self._describe(engine, body.engine_voice_ref, body.engine_options)
        stamp = now_iso()
        self.store.insert_voice(
            {
                "voice_id": body.voice_id,
                "name": body.name,
                "description": body.description,
                "engine_id": body.engine_id,
                "engine_voice_ref": body.engine_voice_ref,
                "language": body.language if body.language is not None else desc["language"],
                "enabled": body.enabled,
                "native_sample_rate": desc["native_sample_rate"],
                "engine_options": body.engine_options,
                "metadata": desc["metadata"],
                "created_at": stamp,
                "updated_at": stamp,
            }
        )
        return self.get(body.voice_id)

    def update(self, voice_id: str, body: VoiceUpdate) -> dict[str, Any]:
        voice = self.get(voice_id)
        changes = body.changes()
        merged = {**voice, **changes}
        engine = self.engines.engine(merged["engine_id"])

        if changes.get("enabled") is False:
            active = self.store.active_profile()
            if active and active["voice_id"] == voice_id:
                raise Conflict(
                    f"voice '{voice_id}' is used by the active profile '{active['name']}'",
                    "voice_in_use",
                    {"profile_id": active["id"]},
                )
        if "engine_id" in changes and changes["engine_id"] != voice["engine_id"]:
            for profile in self.store.profiles_using_voice(voice_id):
                if engine.validate_options(profile["engine_options"], "generation"):
                    raise Conflict(
                        f"profile '{profile['name']}' has engine_options incompatible with engine '{engine.engine_id}'",
                        "profile_options_incompatible",
                        {"profile_id": profile["id"]},
                    )

        values = dict(changes)
        if {"engine_id", "engine_voice_ref", "engine_options"} & changes.keys():
            desc = self._describe(engine, merged["engine_voice_ref"], merged["engine_options"])
            values["native_sample_rate"] = desc["native_sample_rate"]
            values["metadata"] = desc["metadata"]
            if "language" not in changes and desc["language"] and voice["language"] is None:
                values["language"] = desc["language"]
        values["updated_at"] = now_iso()
        self.store.update_voice(voice_id, values)
        return self.get(voice_id)

    def delete(self, voice_id: str) -> None:
        self.get(voice_id)
        users = self.store.profiles_using_voice(voice_id)
        if users:
            raise Conflict(
                f"voice '{voice_id}' is referenced by {len(users)} profile(s)",
                "voice_in_use",
                {"profile_ids": [p["id"] for p in users]},
            )
        self.store.delete_voice(voice_id)

    def validate(self, voice_id: str) -> Report:
        voice = self.get(voice_id)
        engine = self.engines.registry.get(voice["engine_id"])
        if engine is None:
            return Report(errors=[Issue("engine_not_found", f"engine '{voice['engine_id']}' is not installed", "engine_id")])
        report = engine.validate_voice(voice["engine_voice_ref"], voice["engine_options"])
        if not voice["enabled"]:
            report.warnings.append(Issue("voice_disabled", f"voice '{voice_id}' is disabled", "enabled"))
        if not self.engines.state(engine.engine_id)["enabled"]:
            report.warnings.append(Issue("engine_disabled", f"engine '{engine.engine_id}' is disabled", "engine_id"))
        return report


class ProfileService:
    def __init__(self, store: Store, engines: EngineService, voices: VoiceService):
        self.store = store
        self.engines = engines
        self.voices = voices

    def list(self) -> list[dict[str, Any]]:
        return self.store.list_profiles()

    def get(self, profile_id: int) -> dict[str, Any]:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            raise NotFound(f"profile {profile_id} not found", "profile_not_found", {"profile_id": profile_id})
        return profile

    def _voice(self, voice_id: str) -> dict[str, Any]:
        voice = self.store.get_voice(voice_id)
        if voice is None:
            raise NotFound(f"voice '{voice_id}' not found", "voice_not_found", {"voice_id": voice_id})
        return voice

    def _check_options(self, voice: dict[str, Any], options: dict[str, Any]) -> None:
        engine = self.engines.registry.get(voice["engine_id"])
        if engine is None:
            return
        issues = engine.validate_options(options, "generation")
        if issues:
            raise InvalidRequest.from_issues(["body", "engine_options"], issues, options)

    def _check_activatable(self, voice: dict[str, Any]) -> None:
        if not voice["enabled"]:
            raise Conflict(f"voice '{voice['voice_id']}' is disabled", "voice_disabled", {"voice_id": voice["voice_id"]})
        problem = self.engines.usability_problem(voice["engine_id"])
        if problem:
            raise Conflict(problem[1], problem[0], {"engine_id": voice["engine_id"]})

    def _check_name(self, name: str, profile_id: int | None = None) -> None:
        existing = self.store.get_profile_by_name(name)
        if existing and existing["id"] != profile_id:
            raise Conflict(f"profile name '{name}' already exists", "profile_name_exists", {"profile_id": existing["id"]})

    def create(self, body: ProfileCreate) -> dict[str, Any]:
        voice = self._voice(body.voice_id)
        self._check_name(body.name)
        self._check_options(voice, body.engine_options)
        if body.is_active:
            self._check_activatable(voice)
        stamp = now_iso()
        new_id = self.store.insert_profile({**body.model_dump(), "created_at": stamp, "updated_at": stamp})
        return self.get(new_id)

    def update(self, profile_id: int, body: ProfileUpdate) -> dict[str, Any]:
        profile = self.get(profile_id)
        changes = body.changes()
        voice = self._voice(changes.get("voice_id", profile["voice_id"]))
        if "name" in changes:
            self._check_name(changes["name"], profile_id)
        self._check_options(voice, changes.get("engine_options", profile["engine_options"]))
        becomes_active = changes.get("is_active", profile["is_active"])
        if becomes_active and ({"is_active", "voice_id"} & changes.keys()):
            self._check_activatable(voice)
        self.store.update_profile(profile_id, {**changes, "updated_at": now_iso()})
        return self.get(profile_id)

    def delete(self, profile_id: int) -> None:
        profile = self.get(profile_id)
        if profile["is_active"]:
            raise Conflict(
                "the active profile cannot be deleted; activate another profile first",
                "profile_active",
                {"profile_id": profile_id},
            )
        self.store.delete_profile(profile_id)

    def activate(self, profile_id: int) -> dict[str, Any]:
        profile = self.get(profile_id)
        self._check_activatable(self._voice(profile["voice_id"]))
        self.store.update_profile(profile_id, {"is_active": True, "updated_at": now_iso()})
        return self.get(profile_id)
