"""Piper adapter (https://github.com/OHF-Voice/piper1-gpl) using the `piper-tts` Python API."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import threading
from collections import OrderedDict
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


def _range(lo: float, hi: float):
    return lambda v: None if lo <= v <= hi else f"must be between {lo} and {hi}"


class PiperEngine(TtsEngine):
    engine_id = "piper"
    name = "Piper"
    description = "Local neural text-to-speech engine (piper-tts, ONNX voices)"
    option_specs = {
        "use_cuda": OptionSpec((bool,), ("engine",)),
        "voices_dir": OptionSpec((str,), ("engine",), nullable=True),
        "cache_size": OptionSpec((int,), ("engine",), _range(1, 32)),
        "config_path": OptionSpec((str,), ("voice",)),
        "speaker_id": OptionSpec((int,), ("generation",), _range(0, 100000)),
        "speaker": OptionSpec((str,), ("generation",)),
        "noise_scale": OptionSpec((int, float), ("generation",), _range(0.0, 2.0)),
        "noise_w_scale": OptionSpec((int, float), ("generation",), _range(0.0, 2.0)),
        "normalize_audio": OptionSpec((bool,), ("generation",)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._cache: OrderedDict[tuple[str, str, bool], Any] = OrderedDict()
        self._cache_lock = threading.Lock()

    def default_options(self) -> dict[str, Any]:
        return {"use_cuda": False, "voices_dir": None, "cache_size": 4}

    def configure(self, options: Mapping[str, Any]) -> None:
        super().configure(options)
        with self._cache_lock:
            self._cache.clear()

    def availability(self) -> tuple[bool, str | None]:
        if importlib.util.find_spec("piper") is None:
            return False, "piper-tts runtime is not installed (pip install 'opal-talk[piper]')"
        voices_dir = self._options.get("voices_dir")
        if voices_dir and not Path(voices_dir).expanduser().is_dir():
            return True, f"voices_dir '{voices_dir}' does not exist; only absolute voice references will resolve"
        return True, None

    def metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = {"adapter": "opal_talk.tts.piper_adapter", "runtime": "piper-tts"}
        try:
            meta["version"] = importlib.metadata.version("piper-tts")
        except importlib.metadata.PackageNotFoundError:
            meta["version"] = None
        return meta

    def validate(self):
        report = super().validate()
        voices_dir = self._options.get("voices_dir")
        if voices_dir and not Path(voices_dir).expanduser().is_dir():
            report.warnings.append(Issue("voices_dir_missing", f"voices_dir '{voices_dir}' does not exist", "voices_dir"))
        return report

    # -- voice resolution -------------------------------------------------

    def _resolve_paths(self, ref: str, options: Mapping[str, Any]) -> tuple[Path, Path]:
        candidates: list[Path] = []
        ref_path = Path(ref).expanduser()
        if ref_path.is_absolute() or "/" in ref:
            candidates.append(ref_path)
        else:
            voices_dir = self._options.get("voices_dir")
            if voices_dir:
                base = Path(voices_dir).expanduser()
                candidates += [base / ref, base / f"{ref}.onnx"]
        model = next((c for c in candidates if c.is_file()), None)
        if model is None:
            raise VoiceResolutionError(f"Piper voice model '{ref}' could not be resolved on this host", "model_not_found")
        config = Path(options["config_path"]).expanduser() if options.get("config_path") else Path(f"{model}.json")
        if not config.is_file():
            raise VoiceResolutionError(f"Piper voice config '{config}' not found", "model_config_not_found")
        return model, config

    @staticmethod
    def _read_config(config: Path) -> dict[str, Any]:
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VoiceResolutionError(f"Piper voice config '{config}' is unreadable: {exc}", "model_config_invalid") from exc
        if not isinstance(data, dict) or not isinstance(data.get("audio", {}).get("sample_rate"), int):
            raise VoiceResolutionError(f"Piper voice config '{config}' has no audio.sample_rate", "model_config_invalid")
        return data

    def describe_voice(self, ref: str, voice_options: Mapping[str, Any]) -> VoiceDescription:
        model, config_path = self._resolve_paths(ref, voice_options)
        config = self._read_config(config_path)
        language = (config.get("language") or {}).get("code") or config.get("espeak", {}).get("voice")
        speakers = sorted((config.get("speaker_id_map") or {}).keys())
        return VoiceDescription(
            native_sample_rate=config["audio"]["sample_rate"],
            language=language.replace("_", "-") if isinstance(language, str) else None,
            metadata={
                "model_path": str(model),
                "config_path": str(config_path),
                "quality": config["audio"].get("quality"),
                "dataset": config.get("dataset"),
                "num_speakers": config.get("num_speakers", 1),
                "speakers": speakers,
                "piper_version": config.get("piper_version"),
            },
        )

    # -- synthesis --------------------------------------------------------

    def _load(self, model: Path, config: Path):
        if importlib.util.find_spec("piper") is None:
            raise EngineUnavailableError("piper-tts runtime is not installed")
        from piper import PiperVoice

        use_cuda = bool(self._options.get("use_cuda", False))
        key = (str(model), str(config), use_cuda)
        with self._cache_lock:
            voice = self._cache.get(key)
            if voice is not None:
                self._cache.move_to_end(key)
                return voice
        try:
            voice = PiperVoice.load(model, config_path=config, use_cuda=use_cuda)
        except Exception as exc:  # noqa: BLE001 - surface any runtime load failure
            raise SynthesisError(f"failed to load Piper voice '{model}': {exc}") from exc
        with self._cache_lock:
            self._cache[key] = voice
            while len(self._cache) > int(self._options.get("cache_size", 4)):
                self._cache.popitem(last=False)
        return voice

    def synthesize(self, ref: str, text: str, speed: float, options: Mapping[str, Any]) -> Iterator[AudioChunk]:
        from piper import SynthesisConfig

        model, config = self._resolve_paths(ref, options)
        voice = self._load(model, config)
        num_speakers = int(voice.config.num_speakers or 1)

        speaker_id = options.get("speaker_id")
        if options.get("speaker") is not None:
            mapping = voice.config.speaker_id_map or {}
            if options["speaker"] not in mapping:
                raise EngineOptionsError([Issue("invalid_option", f"unknown Piper speaker '{options['speaker']}'", "speaker")])
            speaker_id = mapping[options["speaker"]]
        if speaker_id is not None and speaker_id >= num_speakers:
            raise EngineOptionsError(
                [Issue("invalid_option", f"speaker_id must be < {num_speakers} for this voice", "speaker_id")]
            )

        syn = SynthesisConfig(
            speaker_id=speaker_id if num_speakers > 1 else None,
            length_scale=float(voice.config.length_scale) / speed,
            noise_scale=options.get("noise_scale"),
            noise_w_scale=options.get("noise_w_scale"),
            normalize_audio=bool(options.get("normalize_audio", True)),
            volume=1.0,
        )
        try:
            for chunk in voice.synthesize(text, syn):
                yield AudioChunk(np.asarray(chunk.audio_float_array, dtype=np.float32), int(chunk.sample_rate))
        except Exception as exc:  # noqa: BLE001
            raise SynthesisError(f"Piper synthesis failed: {exc}") from exc
