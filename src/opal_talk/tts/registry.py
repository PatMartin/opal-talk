"""Engine registry. Built-in adapters plus any registered under the `opal_talk.engines` entry-point group."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from importlib.metadata import entry_points

from opal_talk.tts.base import TtsEngine

log = logging.getLogger(__name__)


class EngineRegistry:
    def __init__(self, engines: Iterable[TtsEngine]):
        self._engines: dict[str, TtsEngine] = {}
        for engine in engines:
            self._engines[engine.engine_id] = engine

    def get(self, engine_id: str) -> TtsEngine | None:
        return self._engines.get(engine_id)

    def ids(self) -> list[str]:
        return sorted(self._engines)

    def all(self) -> list[TtsEngine]:
        return [self._engines[k] for k in self.ids()]


def default_engines() -> list[TtsEngine]:
    from opal_talk.tts.kokoro_adapter import KokoroEngine
    from opal_talk.tts.piper_adapter import PiperEngine

    engines: list[TtsEngine] = [PiperEngine(), KokoroEngine()]
    for ep in entry_points(group="opal_talk.engines"):
        try:
            engines.append(ep.load()())
        except Exception:  # noqa: BLE001 - a broken plugin must not take the service down
            log.exception("failed to load TTS engine plugin %s", ep.name)
    return engines
