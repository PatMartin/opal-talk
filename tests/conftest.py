from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from contract_check import check_response
from fastapi.testclient import TestClient

from opal_talk.api.app import create_app
from opal_talk.config import ServiceConfig
from opal_talk.tts.base import (
    AudioChunk,
    EngineOptionsError,
    Issue,
    OptionSpec,
    SynthesisError,
    TtsEngine,
    VoiceDescription,
    VoiceResolutionError,
)

PIPER_MODEL = Path(os.environ.get("OPAL_TALK_TEST_PIPER_MODEL", "~/piper/voices/en_US-amy-low.onnx")).expanduser()
KOKORO_DIR = Path(os.environ.get("OPAL_TALK_TEST_KOKORO_DIR", "~/kokoro")).expanduser()
KOKORO_MODEL = next(iter(sorted(KOKORO_DIR.glob("kokoro-v1.0*.onnx"))), KOKORO_DIR / "kokoro-v1.0.onnx")
KOKORO_VOICES = KOKORO_DIR / "voices-v1.0.bin"


def piper_ready() -> bool:
    import importlib.util

    return importlib.util.find_spec("piper") is not None and PIPER_MODEL.is_file()


def kokoro_ready() -> bool:
    import importlib.util

    return importlib.util.find_spec("kokoro_onnx") is not None and KOKORO_MODEL.is_file() and KOKORO_VOICES.is_file()


class FakeEngine(TtsEngine):
    """Deterministic tone generator: one audio block per text chunk, length proportional to text/speed."""

    name = "Fake"
    description = "Deterministic test engine"
    VOICES = {"tone-a": 22050, "tone-b": 16000}
    option_specs = {
        "available": OptionSpec((bool,), ("engine",)),
        "model_dir": OptionSpec((str,), ("voice",)),
        "pitch": OptionSpec((int, float), ("generation",), lambda v: None if 50 <= v <= 2000 else "must be 50..2000"),
        "fail_on_call": OptionSpec((int,), ("generation",)),
        "strict": OptionSpec((bool,), ("generation",)),
        "delay": OptionSpec((int, float), ("generation",)),
    }

    def __init__(self, engine_id: str = "fake", speed_range: tuple[float, float] = (0.25, 4.0),
                 voices: dict[str, int] | None = None):
        super().__init__()
        self.engine_id = engine_id
        self.speed_range = speed_range
        self.VOICES = voices or dict(FakeEngine.VOICES)
        self.calls: list[dict[str, Any]] = []

    def default_options(self) -> dict[str, Any]:
        return {"available": True}

    def availability(self) -> tuple[bool, str | None]:
        if self._options.get("available", True):
            return True, None
        return False, "fake runtime disabled by options"

    def metadata(self) -> dict[str, Any]:
        return {"version": "0.0-test"}

    def describe_voice(self, ref: str, voice_options: Mapping[str, Any]) -> VoiceDescription:
        if ref not in self.VOICES:
            raise VoiceResolutionError(f"unknown fake voice '{ref}'", "model_not_found")
        return VoiceDescription(self.VOICES[ref], "en-US", {"kind": "tone"})

    def synthesize(self, ref: str, text: str, speed: float, options: Mapping[str, Any]) -> Iterator[AudioChunk]:
        if options.get("strict") and options.get("pitch") == 999:
            raise EngineOptionsError([Issue("invalid_option", "pitch 999 is not allowed in strict mode", "pitch")])
        self.calls.append({"ref": ref, "text": text, "speed": speed, "options": dict(options)})
        if options.get("fail_on_call") == len(self.calls):
            raise SynthesisError("synthetic failure")
        if options.get("delay"):
            time.sleep(options["delay"])
        rate = self.VOICES[ref]
        n = max(1, int(rate * 0.01 * len(text) / speed))
        t = np.arange(n) / rate
        pitch = float(options.get("pitch", 440))
        yield AudioChunk((0.5 * np.sin(2 * math.pi * pitch * t)).astype(np.float32), rate)


class ContractClient(TestClient):
    """TestClient that checks every response against opal_talk.yaml."""

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        resp = super().request(method, url, *args, **kwargs)
        if not getattr(self, "skip_contract", False):
            check_response(resp)
        return resp


@pytest.fixture
def config(tmp_path: Path) -> ServiceConfig:
    return ServiceConfig(data_dir=tmp_path / "var", config_dir=tmp_path / "config")


@pytest.fixture
def engines() -> list[FakeEngine]:
    return [FakeEngine("fake"), FakeEngine("alt", speed_range=(0.5, 2.0), voices={"tone-a": 24000})]


@pytest.fixture
def app(config: ServiceConfig, engines: list[FakeEngine]):
    application = create_app(config, engines=engines)
    yield application
    application.state.services.close()


@pytest.fixture
def client(app) -> Iterator[ContractClient]:
    with ContractClient(app) as c:
        yield c


@pytest.fixture
def live_server(app) -> Iterator[str]:
    """Run the app under a real uvicorn server (needed to observe true HTTP streaming behaviour)."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def make_voice(client: TestClient, voice_id: str = "v1", engine_id: str = "fake", ref: str = "tone-a", **extra) -> dict:
    resp = client.post(
        "/api/v1/voices/",
        json={"voice_id": voice_id, "name": voice_id.upper(), "engine_id": engine_id, "engine_voice_ref": ref, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_profile(client: TestClient, name: str = "p1", voice_id: str = "v1", **extra) -> dict:
    resp = client.post("/api/v1/profiles/", json={"name": name, "voice_id": voice_id, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()
