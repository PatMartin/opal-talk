"""Functional tests for the real Piper and Kokoro adapters (skipped when the host lacks them),
plus REST -> service -> real adapter integration."""

from __future__ import annotations

import importlib.util
import io
import shutil
import wave

import numpy as np
import pytest
from contract_check import check_response
from fastapi.testclient import TestClient

from conftest import KOKORO_MODEL, KOKORO_VOICES, PIPER_MODEL, kokoro_ready, piper_ready
from opal_talk.api.app import create_app
from opal_talk.config import ServiceConfig
from opal_talk.tts.base import EngineOptionsError, VoiceResolutionError
from opal_talk.tts.kokoro_adapter import KokoroEngine
from opal_talk.tts.piper_adapter import PiperEngine

needs_piper = pytest.mark.skipif(not piper_ready(), reason=f"Piper runtime or model {PIPER_MODEL} not available")
needs_kokoro = pytest.mark.skipif(not kokoro_ready(), reason="kokoro-onnx runtime or model files not available")


def _duration(chunks) -> float:
    return sum(len(c.samples) / c.sample_rate for c in chunks)


@pytest.fixture(scope="module")
def piper_engine():
    e = PiperEngine()
    e.configure({"voices_dir": str(PIPER_MODEL.parent), "use_cuda": False, "cache_size": 2})
    return e


@pytest.fixture(scope="module")
def kokoro_engine():
    e = KokoroEngine()
    e.configure({"model_path": str(KOKORO_MODEL), "voices_path": str(KOKORO_VOICES), "default_lang": "en-us"})
    return e


# -- Piper -------------------------------------------------------------------------


class TestPiperOptions:
    def test_option_scopes(self):
        e = PiperEngine()
        assert e.validate_options({"use_cuda": False, "voices_dir": None, "cache_size": 2}, "engine") == []
        assert e.validate_options({"config_path": "/x.json", "noise_scale": 0.5}, "voice") == []
        assert e.validate_options({"speaker_id": 1, "noise_w_scale": 0.8, "normalize_audio": False}, "generation") == []
        assert [i.field for i in e.validate_options({"config_path": "/x"}, "generation")] == ["config_path"]
        assert [i.field for i in e.validate_options({"length_scale": 1.0}, "generation")] == ["length_scale"]
        assert [i.field for i in e.validate_options({"noise_scale": 5}, "generation")] == ["noise_scale"]

    def test_missing_model(self, tmp_path):
        e = PiperEngine()
        e.configure({"voices_dir": str(tmp_path)})
        with pytest.raises(VoiceResolutionError) as exc:
            e.describe_voice("nope", {})
        assert exc.value.code == "model_not_found"
        (tmp_path / "m.onnx").write_bytes(b"x")
        with pytest.raises(VoiceResolutionError) as exc:
            e.describe_voice("m", {})
        assert exc.value.code == "model_config_not_found"
        (tmp_path / "m.onnx.json").write_text("{not json")
        with pytest.raises(VoiceResolutionError) as exc:
            e.describe_voice("m", {})
        assert exc.value.code == "model_config_invalid"


@needs_piper
@pytest.mark.piper
class TestPiperAdapter:
    @pytest.fixture
    def engine(self, piper_engine):
        return piper_engine

    def test_availability_and_metadata(self, engine):
        assert engine.availability() == (True, None)
        assert engine.metadata()["version"]
        assert engine.validate().valid

    def test_describe_by_path_and_name(self, engine):
        by_path = engine.describe_voice(str(PIPER_MODEL), {})
        by_name = engine.describe_voice(PIPER_MODEL.name.removesuffix(".onnx"), {})
        assert by_path == by_name
        assert by_path.native_sample_rate in (16000, 22050, 24000, 44100)
        assert by_path.metadata["num_speakers"] >= 1

    def test_synthesize_speed(self, engine):
        ref = str(PIPER_MODEL)
        rate = engine.native_sample_rate(ref, {})
        normal = list(engine.synthesize(ref, "The quick brown fox jumps over the lazy dog.", 1.0, {}))
        fast = list(engine.synthesize(ref, "The quick brown fox jumps over the lazy dog.", 2.0, {"noise_scale": 0.5}))
        assert all(c.sample_rate == rate and c.samples.dtype == np.float32 for c in normal)
        assert np.abs(np.concatenate([c.samples for c in normal])).max() > 0.05
        # Phoneme durations halve; fixed padding/silence keeps the ratio a bit above 0.5.
        assert 0.35 < _duration(fast) / _duration(normal) < 0.75

    def test_invalid_speaker(self, engine):
        meta = engine.describe_voice(str(PIPER_MODEL), {}).metadata
        if meta["num_speakers"] > 1:
            pytest.skip("multi-speaker model")
        with pytest.raises(EngineOptionsError):
            list(engine.synthesize(str(PIPER_MODEL), "hi", 1.0, {"speaker_id": 3}))
        with pytest.raises(EngineOptionsError):
            list(engine.synthesize(str(PIPER_MODEL), "hi", 1.0, {"speaker": "nobody"}))


# -- Kokoro ------------------------------------------------------------------------


class TestKokoroOptions:
    def test_unconfigured_is_unavailable(self):
        e = KokoroEngine()
        e.configure(e.default_options())
        available, message = e.availability()
        assert available is False
        # With the runtime installed, the message must point at the missing options.
        if importlib.util.find_spec("kokoro_onnx") is not None:
            assert "model_path" in message

    def test_missing_files(self, tmp_path):
        e = KokoroEngine()
        e.configure({"model_path": str(tmp_path / "m.onnx"), "voices_path": str(tmp_path / "v.bin")})
        assert e.availability()[0] is False
        assert not e.validate().valid

    def test_option_scopes(self):
        e = KokoroEngine()
        assert e.validate_options({"lang": "en-gb"}, "generation") == []
        assert e.validate_options({"lang": "klingon"}, "generation")[0].field == "lang"
        assert e.validate_options({"model_path": "/x"}, "generation")[0].code == "unsupported_option"


@needs_kokoro
@pytest.mark.kokoro
class TestKokoroAdapter:
    @pytest.fixture
    def engine(self, kokoro_engine):
        return kokoro_engine

    def test_availability_and_validate(self, engine):
        assert engine.availability() == (True, None)
        report = engine.validate()
        assert report.valid and report.details["voice_count"] > 10

    def test_describe(self, engine):
        desc = engine.describe_voice("bf_emma", {})
        assert desc.native_sample_rate == 24000 and desc.language == "en-gb"
        with pytest.raises(VoiceResolutionError):
            engine.describe_voice("zz_nobody", {})

    def test_synthesize_and_speed_limits(self, engine):
        chunks = list(engine.synthesize("af_heart", "Hello world.", 1.0, {}))
        assert chunks[0].sample_rate == 24000 and _duration(chunks) > 0.3
        with pytest.raises(EngineOptionsError):
            list(engine.synthesize("af_heart", "Hello", 3.0, {}))


# -- integration: REST -> service -> real adapter -----------------------------------------


def _real_app(tmp_path, engines):
    cfg = ServiceConfig(data_dir=tmp_path / "var", config_dir=tmp_path / "config", engines=engines)
    return create_app(cfg)


@needs_piper
@pytest.mark.piper
def test_rest_to_piper_end_to_end(tmp_path):
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    for suffix in ("", ".json"):
        shutil.copy(f"{PIPER_MODEL}{suffix}", voices_dir / f"test-voice.onnx{suffix}")
    app = _real_app(tmp_path, {"piper": {"options": {"voices_dir": str(voices_dir)}}, "kokoro": {"enabled": False}})
    with TestClient(app) as c:
        resp = c.post("/api/v1/voices/", json={"voice_id": "p", "name": "P", "engine_id": "piper",
                                                "engine_voice_ref": "test-voice"})
        assert resp.status_code == 201, resp.text
        native = resp.json()["native_sample_rate"]
        assert c.post("/api/v1/voices/p/validate").json()["valid"] is True

        resp = c.post("/api/v1/generate", json={"text": "Hello from OPAL Talk. Second sentence.", "voice_id": "p"})
        check_response(resp)
        assert resp.headers["x-opal-sample-rate"] == str(native)
        with wave.open(io.BytesIO(resp.content)) as w:
            assert w.getframerate() == native and w.getnframes() > native * 0.5

        with c.stream("POST", "/api/v1/generate/stream",
                      json={"text": "Streaming from Piper.", "voice_id": "p", "sample_rate": 8000}) as resp:
            pcm = resp.read()
        assert resp.headers["x-opal-sample-rate"] == "8000"
        assert np.abs(np.frombuffer(pcm, "<i2")).max() > 1000

        resp = c.post("/api/v1/voices/p/test", json={"output_format": "pcm", "engine_options": {"bogus": 1}})
        assert resp.status_code == 422
        # Unregistering never touches host files.
        assert c.delete("/api/v1/voices/p").status_code == 204
        assert (voices_dir / "test-voice.onnx").is_file()
    app.state.services.close()


@needs_kokoro
@pytest.mark.kokoro
def test_rest_to_kokoro_end_to_end(tmp_path):
    app = _real_app(tmp_path, {"kokoro": {"options": {"model_path": str(KOKORO_MODEL),
                                                       "voices_path": str(KOKORO_VOICES)}}})
    with TestClient(app) as c:
        resp = c.post("/api/v1/voices/", json={"voice_id": "k", "name": "K", "engine_id": "kokoro",
                                                "engine_voice_ref": "am_adam"})
        assert resp.status_code == 201 and resp.json()["native_sample_rate"] == 24000
        resp = c.post("/api/v1/generate", json={"text": "Hi.", "voice_id": "k", "output_format": "pcm",
                                                 "sample_rate": 16000, "engine_options": {"lang": "en-us"}})
        check_response(resp)
        assert resp.headers["x-opal-sample-rate"] == "16000" and len(resp.content) > 16000 * 2 * 0.2
        resp = c.post("/api/v1/generate", json={"text": "Hi.", "voice_id": "k", "speed": 3.0})
        assert resp.status_code == 409 and resp.json()["code"] == "unsupported_speed"
    app.state.services.close()
