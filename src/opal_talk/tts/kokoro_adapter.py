"""Kokoro adapter using the `kokoro-onnx` runtime with host-installed model and voices files."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from opal_talk.tts.base import (
    AudioChunk,
    EngineOptionsError,
    EngineUnavailableError,
    Issue,
    OptionSpec,
    SynthesisError,
    TtsEngine,
    VoiceDescription,
    VoiceResolutionError,
)

KOKORO_SAMPLE_RATE = 24000
# Kokoro voice-name prefix -> espeak language code understood by kokoro-onnx.
LANG_BY_PREFIX = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}
SUPPORTED_LANGS = frozenset(LANG_BY_PREFIX.values())


def _lang_check(value: str) -> str | None:
    return None if value in SUPPORTED_LANGS else f"must be one of {sorted(SUPPORTED_LANGS)}"


class KokoroEngine(TtsEngine):
    engine_id = "kokoro"
    name = "Kokoro"
    description = "Kokoro-82M neural text-to-speech via kokoro-onnx"
    speed_range = (0.5, 2.0)
    option_specs = {
        "model_path": OptionSpec((str,), ("engine",), nullable=True),
        "voices_path": OptionSpec((str,), ("engine",), nullable=True),
        "default_lang": OptionSpec((str,), ("engine",), _lang_check),
        "lang": OptionSpec((str,), ("generation",), _lang_check),
    }

    def __init__(self) -> None:
        super().__init__()
        self._kokoro: Any = None
        self._voice_names: frozenset[str] | None = None
        self._lock = threading.Lock()

    def default_options(self) -> dict[str, Any]:
        return {"model_path": None, "voices_path": None, "default_lang": "en-us"}

    def configure(self, options: Mapping[str, Any]) -> None:
        super().configure(options)
        with self._lock:
            self._kokoro = None
            self._voice_names = None

    def _paths(self) -> tuple[Path | None, Path | None]:
        model = self._options.get("model_path")
        voices = self._options.get("voices_path")
        return (Path(model).expanduser() if model else None, Path(voices).expanduser() if voices else None)

    def availability(self) -> tuple[bool, str | None]:
        if importlib.util.find_spec("kokoro_onnx") is None:
            return False, "kokoro-onnx runtime is not installed (pip install 'opal-talk[kokoro]')"
        model, voices = self._paths()
        if model is None or voices is None:
            return False, "engine options 'model_path' and 'voices_path' must be configured"
        if not model.is_file():
            return False, f"Kokoro model file '{model}' not found"
        if not voices.is_file():
            return False, f"Kokoro voices file '{voices}' not found"
        return True, None

    def metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "adapter": "opal_talk.tts.kokoro_adapter",
            "runtime": "kokoro-onnx",
            "native_sample_rate": KOKORO_SAMPLE_RATE,
            "speed_range": list(self.speed_range),
        }
        try:
            meta["version"] = importlib.metadata.version("kokoro-onnx")
        except importlib.metadata.PackageNotFoundError:
            meta["version"] = None
        return meta

    def validate(self):
        report = super().validate()
        if report.valid:
            try:
                report.details["voice_count"] = len(self._voices())
                self._engine()
            except (EngineUnavailableError, SynthesisError) as exc:
                report.errors.append(Issue("engine_load_failed", str(exc)))
        return report

    def _voices(self) -> frozenset[str]:
        available, message = self.availability()
        if not available:
            raise EngineUnavailableError(message or "Kokoro is unavailable")
        with self._lock:
            if self._voice_names is None:
                _, voices = self._paths()
                try:
                    with np.load(voices) as data:
                        self._voice_names = frozenset(data.files)
                except Exception as exc:  # noqa: BLE001
                    raise EngineUnavailableError(f"Kokoro voices file '{voices}' is unreadable: {exc}") from exc
            return self._voice_names

    def _engine(self):
        available, message = self.availability()
        if not available:
            raise EngineUnavailableError(message or "Kokoro is unavailable")
        with self._lock:
            if self._kokoro is None:
                from kokoro_onnx import Kokoro

                model, voices = self._paths()
                try:
                    self._kokoro = Kokoro(str(model), str(voices))
                except Exception as exc:  # noqa: BLE001
                    raise SynthesisError(f"failed to load Kokoro model: {exc}") from exc
            return self._kokoro

    def describe_voice(self, ref: str, voice_options: Mapping[str, Any]) -> VoiceDescription:
        if ref not in self._voices():
            raise VoiceResolutionError(f"Kokoro voice '{ref}' is not present in the configured voices file", "voice_not_found")
        lang = voice_options.get("lang") or LANG_BY_PREFIX.get(ref[:1])
        return VoiceDescription(
            native_sample_rate=KOKORO_SAMPLE_RATE,
            language=lang,
            metadata={"kokoro_voice": ref, "lang": lang, "gender": {"f": "female", "m": "male"}.get(ref[1:2])},
        )

    def synthesize(self, ref: str, text: str, speed: float, options: Mapping[str, Any]) -> Iterator[AudioChunk]:
        if ref not in self._voices():
            raise VoiceResolutionError(f"Kokoro voice '{ref}' is not present in the configured voices file", "voice_not_found")
        lo, hi = self.speed_range
        if not lo <= speed <= hi:
            raise EngineOptionsError([Issue("unsupported_speed", f"Kokoro supports speed {lo}..{hi}", "speed")])
        lang = options.get("lang") or LANG_BY_PREFIX.get(ref[:1]) or self._options.get("default_lang", "en-us")
        kokoro = self._engine()
        try:
            samples, rate = kokoro.create(text, voice=ref, speed=float(speed), lang=lang)
        except Exception as exc:  # noqa: BLE001
            raise SynthesisError(f"Kokoro synthesis failed: {exc}") from exc
        yield AudioChunk(np.asarray(samples, dtype=np.float32).reshape(-1), int(rate))
