"""Candidate configuration: export, validation, atomic apply and YAML import.

A `Candidate` is a proposed configuration (None sections mean "keep current"). It is
validated as a whole — cross-references, engine option rules, host resource checks —
and only then applied in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from opal_talk import __version__
from opal_talk.persistence.store import Store, now_iso
from opal_talk.schemas import ProfileCreate, VoiceCreate
from opal_talk.service.catalog import EngineService
from opal_talk.service.errors import Conflict, OperationFailed
from opal_talk.service.settings import setting_problem, settings_to_str
from opal_talk.tts.base import EngineUnavailableError, Issue, Report, TtsEngine, VoiceResolutionError

CONFIG_FILES = ("engines.yaml", "voices.yaml", "profiles.yaml", "settings.yaml")


@dataclass
class Candidate:
    """A proposed configuration; `None` sections mean "keep current"."""

    engines: dict[str, dict[str, Any]] | None = None
    voices: list[dict[str, Any]] | None = None
    profiles: list[dict[str, Any]] | None = None
    settings: dict[str, str] | None = None


def _apply_summary(cand: Candidate) -> str:
    """Human-readable summary of what applying `cand` does to existing state."""
    parts = []
    for name, section in (("voices", cand.voices), ("profiles", cand.profiles), ("settings", cand.settings)):
        if section is not None:
            parts.append(f"replaced {name} ({len(section)})")
    if cand.engines is not None:
        parts.append(f"applied engine options ({len(cand.engines)})")
    return "; ".join(parts)


def _schema_issues(exc: ValidationError, prefix: str) -> list[Issue]:
    return [
        Issue("schema_invalid", f"{prefix}: {err['msg']}", ".".join([prefix, *map(str, err["loc"])]))
        for err in exc.errors()
    ]


class CandidateService:
    """Validates and applies whole configuration candidates (import/export/restore share this)."""

    def __init__(self, store: Store, engines: EngineService, config_dir: Path):
        self.store = store
        self.engines = engines
        self.config_dir = config_dir

    # -- export ---------------------------------------------------------------

    def export(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "exported_at": now_iso(),
            "engines": self.engines.list(),
            "voices": self.store.list_voices(),
            "profiles": self.store.list_profiles(),
            "settings": {r["key"]: r["value"] for r in self.store.list_settings()},
        }

    # -- validation -------------------------------------------------------------

    def _probe(self, engine: TtsEngine, options: dict[str, Any]) -> TtsEngine:
        """Engine instance configured with candidate options, without disturbing the live adapter."""
        if options == engine.options:
            return engine
        try:
            probe = type(engine)()
        except TypeError:
            return engine
        probe.configure(options)
        return probe

    def validate_candidate(self, cand: Candidate, resources_are_errors: bool) -> tuple[Report, dict[str, dict]]:
        """Validate cross-references and engine rules. Returns the report and resolved voice descriptions."""
        report = Report()
        resource_bucket = report.errors if resources_are_errors else report.warnings

        engine_states = {s["engine_id"]: s for s in self.store.list_engine_states()}
        for engine_id, state in (cand.engines or {}).items():
            engine = self.engines.runtime.get(engine_id)
            if engine is None:
                report.warnings.append(
                    Issue("engine_not_installed", f"engine '{engine_id}' is not installed; its configuration is ignored",
                          f"engines.{engine_id}")
                )
                continue
            engine_states[engine_id] = {"engine_id": engine_id, **state}
        for engine_id, state in engine_states.items():
            engine = self.engines.runtime.get(engine_id)
            if engine is None:
                continue
            for issue in engine.validate_options(state["options"], "engine"):
                report.errors.append(Issue(issue.code, issue.message, f"engines.{engine_id}.options.{issue.field}"))

        voices = cand.voices if cand.voices is not None else self.store.list_voices()
        descriptions: dict[str, dict] = {}
        seen: set[str] = set()
        probes: dict[str, TtsEngine] = {}
        for voice in voices:
            vid = voice["voice_id"]
            field = f"voices.{vid}"
            if vid in seen:
                report.errors.append(Issue("duplicate_voice", f"voice '{vid}' is defined more than once", field))
                continue
            seen.add(vid)
            engine = self.engines.runtime.get(voice["engine_id"])
            if engine is None:
                report.errors.append(
                    Issue("engine_not_found", f"voice '{vid}' references unknown engine '{voice['engine_id']}'",
                          f"{field}.engine_id")
                )
                continue
            issues = engine.validate_options(voice.get("engine_options") or {}, "voice")
            for issue in issues:
                report.errors.append(Issue(issue.code, issue.message, f"{field}.engine_options.{issue.field}"))
            if issues:
                continue
            state = engine_states.get(engine.engine_id, {"options": engine.options})
            probe = probes.setdefault(engine.engine_id, self._probe(engine, state["options"]))
            available, message = probe.availability()
            if not available:
                report.warnings.append(
                    Issue("voice_not_checked", f"voice '{vid}' not checked: {message}", f"{field}.engine_voice_ref")
                )
                continue
            try:
                desc = probe.describe_voice(voice["engine_voice_ref"], voice.get("engine_options") or {})
            except (VoiceResolutionError, EngineUnavailableError) as exc:
                resource_bucket.append(Issue(getattr(exc, "code", "voice_unresolvable"), str(exc), f"{field}.engine_voice_ref"))
                continue
            descriptions[vid] = {"native_sample_rate": desc.native_sample_rate, "metadata": desc.metadata,
                                 "language": desc.language}

        voice_by_id = {v["voice_id"]: v for v in voices}
        profiles = cand.profiles if cand.profiles is not None else self.store.list_profiles()
        names: set[str] = set()
        active = [p for p in profiles if p.get("is_active")]
        if len(active) > 1:
            report.errors.append(Issue("multiple_active_profiles", "more than one profile is marked active", "profiles"))
        if profiles and not active:
            report.warnings.append(Issue("no_active_profile", "no profile is active", "profiles"))
        for profile in profiles:
            field = f"profiles.{profile['name']}"
            if profile["name"] in names:
                report.errors.append(Issue("duplicate_profile", f"profile name '{profile['name']}' is duplicated", field))
            names.add(profile["name"])
            voice = voice_by_id.get(profile["voice_id"])
            if voice is None:
                report.errors.append(
                    Issue("voice_not_found", f"profile '{profile['name']}' references unknown voice '{profile['voice_id']}'",
                          f"{field}.voice_id")
                )
                continue
            if profile.get("is_active") and not voice.get("enabled", True):
                report.warnings.append(Issue("voice_disabled", f"active profile uses disabled voice '{voice['voice_id']}'",
                                             f"{field}.voice_id"))
            engine = self.engines.runtime.get(voice["engine_id"])
            if engine is not None:
                for issue in engine.validate_options(profile.get("engine_options") or {}, "generation"):
                    report.errors.append(Issue(issue.code, issue.message, f"{field}.engine_options.{issue.field}"))

        settings = cand.settings if cand.settings is not None else {
            r["key"]: r["value"] for r in self.store.list_settings()
        }
        for key, value in settings.items():
            problem = setting_problem(key, value)
            if problem:
                report.errors.append(Issue("invalid_setting", f"setting '{key}' {problem}", f"settings.{key}"))

        report.details = {
            "engines": len(engine_states),
            "voices": len(voices),
            "profiles": len(profiles),
            "settings": len(settings),
        }
        return report, descriptions

    def validate_current(self) -> Report:
        return self.validate_candidate(Candidate(), resources_are_errors=True)[0]

    # -- apply ----------------------------------------------------------------

    def apply(self, cand: Candidate, descriptions: dict[str, dict]) -> None:
        engines = None
        if cand.engines is not None:
            engines = {k: v for k, v in cand.engines.items() if self.engines.runtime.get(k) is not None}
        voices = None
        if cand.voices is not None:
            voices = []
            for v in cand.voices:
                desc = descriptions.get(v["voice_id"], {})
                row = {
                    "voice_id": v["voice_id"],
                    "name": v["name"],
                    "description": v.get("description"),
                    "engine_id": v["engine_id"],
                    "engine_voice_ref": v["engine_voice_ref"],
                    "language": v.get("language") or desc.get("language"),
                    "enabled": v.get("enabled", True),
                    "native_sample_rate": desc.get("native_sample_rate", v.get("native_sample_rate")),
                    "engine_options": v.get("engine_options") or {},
                    "metadata": desc.get("metadata", v.get("metadata") or {}),
                }
                for stamp in ("created_at", "updated_at"):
                    if v.get(stamp):
                        row[stamp] = v[stamp]
                voices.append(row)
        profiles = None
        if cand.profiles is not None:
            profiles = []
            fields = ("name", "description", "is_active", "voice_id", "speed", "volume", "output_format",
                      "sample_rate", "engine_options")
            for index, p in enumerate(cand.profiles, start=1):
                row = {k: p.get(k) for k in fields}
                row["id"] = p.get("id") or index
                for stamp in ("created_at", "updated_at"):
                    if p.get(stamp):
                        row[stamp] = p[stamp]
                profiles.append(row)
        self.store.replace_all(engines, voices, profiles, cand.settings)
        for engine in self.engines.runtime.all():
            state = self.store.get_engine_state(engine.engine_id)
            if state is not None:
                engine.configure(state["options"])

    def summary(self, cand: Candidate) -> str:
        return _apply_summary(cand)

    # -- import from ./config ----------------------------------------------------

    def _load_yaml(self, name: str, section: str, issues: list[Issue]) -> Any:
        path = self.config_dir / name
        if not path.is_file():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            issues.append(Issue("yaml_invalid", f"{name}: {exc}", name))
            return None
        if not isinstance(data, dict) or section not in data:
            issues.append(Issue("yaml_invalid", f"{name}: expected a mapping with a '{section}' key", name))
            return None
        return data[section] if data[section] is not None else ({} if section in ("engines", "settings") else [])

    def _parse_list(self, items: Any, model: type[BaseModel], prefix: str, issues: list[Issue]) -> list[dict]:
        if not isinstance(items, list):
            issues.append(Issue("yaml_invalid", f"{prefix} must be a list", prefix))
            return []
        out = []
        for index, item in enumerate(items):
            try:
                out.append(model.model_validate(item).model_dump())
            except ValidationError as exc:
                issues.extend(_schema_issues(exc, f"{prefix}[{index}]"))
        return out

    def import_config(self) -> dict[str, Any]:
        present = [n for n in CONFIG_FILES if (self.config_dir / n).is_file()]
        if not present:
            raise Conflict(
                f"no configuration files found in '{self.config_dir}' (expected any of {list(CONFIG_FILES)})",
                "config_not_found",
                {"config_dir": str(self.config_dir)},
            )
        issues: list[Issue] = []
        cand = Candidate()

        raw_engines = self._load_yaml("engines.yaml", "engines", issues)
        if raw_engines is not None:
            if not isinstance(raw_engines, dict):
                issues.append(Issue("yaml_invalid", "engines must be a mapping of engine_id to settings", "engines"))
            else:
                cand.engines = {}
                for engine_id, cfg in raw_engines.items():
                    cfg = cfg or {}
                    engine = self.engines.runtime.get(str(engine_id))
                    defaults = engine.default_options() if engine else {}
                    if not isinstance(cfg.get("options", {}), dict) or not isinstance(cfg.get("enabled", True), bool):
                        issues.append(Issue("schema_invalid", "enabled must be boolean and options a mapping",
                                            f"engines.{engine_id}"))
                        continue
                    cand.engines[str(engine_id)] = {
                        "enabled": cfg.get("enabled", True),
                        "options": {**defaults, **(cfg.get("options") or {})},
                    }
        raw_voices = self._load_yaml("voices.yaml", "voices", issues)
        if raw_voices is not None:
            cand.voices = self._parse_list(raw_voices, VoiceCreate, "voices", issues)
        raw_profiles = self._load_yaml("profiles.yaml", "profiles", issues)
        if raw_profiles is not None:
            cand.profiles = self._parse_list(raw_profiles, ProfileCreate, "profiles", issues)
        raw_settings = self._load_yaml("settings.yaml", "settings", issues)
        if raw_settings is not None:
            if isinstance(raw_settings, dict):
                cand.settings = settings_to_str(raw_settings)
            else:
                issues.append(Issue("yaml_invalid", "settings must be a mapping", "settings"))

        report, descriptions = self.validate_candidate(cand, resources_are_errors=False)
        report.errors[:0] = issues
        report.details["files"] = present
        if not report.valid:
            return {"success": False, "validation": report, "message": "Configuration is invalid; nothing was applied."}
        try:
            self.apply(cand, descriptions)
        except Exception as exc:  # noqa: BLE001
            raise OperationFailed(f"failed to apply configuration: {exc}", "import_failed") from exc
        return {
            "success": True,
            "validation": report,
            "message": f"Configuration imported successfully: {_apply_summary(cand)}.",
        }
