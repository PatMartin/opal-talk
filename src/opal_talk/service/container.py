"""Service container and readiness reporting."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from opal_talk import __version__
from opal_talk.config import ServiceConfig
from opal_talk.persistence.store import Store
from opal_talk.service.catalog import EngineService, ProfileService, VoiceService
from opal_talk.service.config_ops import ConfigService
from opal_talk.service.errors import ApiError
from opal_talk.service.generation import GenerationService
from opal_talk.service.settings import SettingsService
from opal_talk.tts.base import TtsEngine
from opal_talk.tts.registry import EngineRegistry, default_engines
from opal_talk.tts.runtime import EngineRuntimeManager

log = logging.getLogger(__name__)


@dataclass
class Services:
    config: ServiceConfig
    store: Store
    runtime: EngineRuntimeManager
    engines: EngineService
    voices: VoiceService
    profiles: ProfileService
    settings: SettingsService
    generation: GenerationService
    config_ops: ConfigService

    def health(self) -> dict[str, Any]:
        engines = []
        for engine in self.runtime.all():
            available, message = engine.availability()
            enabled = self.engines.state(engine.engine_id)["enabled"]
            engines.append({"engine_id": engine.engine_id, "enabled": enabled, "available": available,
                            "message": message})
        usable = [e for e in engines if e["enabled"] and e["available"]]
        broken = [e for e in engines if e["enabled"] and not e["available"]]
        status = "error" if not usable else ("degraded" if broken else "ok")
        active = self.store.active_profile()
        return {
            "status": status,
            "version": __version__,
            "active_profile_id": active["id"] if active else None,
            "engines": engines,
            "details": {
                "voices": len(self.store.list_voices()),
                "enabled_voices": len(self.store.list_voices(enabled=True)),
                "profiles": len(self.store.list_profiles()),
            },
        }

    def close(self) -> None:
        self.store.close()


def build_services(config: ServiceConfig, engines: list[TtsEngine] | None = None) -> Services:
    store = Store(config.db_path)
    fresh = store.created
    runtime = EngineRuntimeManager(EngineRegistry(engines if engines is not None else default_engines()))
    engine_svc = EngineService(store, runtime)
    engine_svc.bootstrap(config.engines)
    voice_svc = VoiceService(store, engine_svc)
    settings_svc = SettingsService(store)
    services = Services(
        config=config,
        store=store,
        runtime=runtime,
        engines=engine_svc,
        voices=voice_svc,
        profiles=ProfileService(store, engine_svc, voice_svc),
        settings=settings_svc,
        generation=GenerationService(store, engine_svc, settings_svc),
        config_ops=ConfigService(store, engine_svc, config.config_dir, config.backup_dir),
    )
    if fresh and config.config_dir.is_dir():
        try:
            result = services.config_ops.import_config()
            if not result["success"]:
                log.warning("initial config import skipped: %s", [i.message for i in result["validation"].errors])
        except ApiError as exc:
            log.info("no initial configuration imported: %s", exc.message)
    return services
