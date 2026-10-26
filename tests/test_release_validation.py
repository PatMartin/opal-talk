"""Release validation against real Piper and Kokoro engines.

Skipped cleanly unless the runtime packages and model files are installed —
see docs/RELEASE_VALIDATION.md for required env vars and per-platform commands.
Nothing here runs in standard CI (`-m "not piper and not kokoro"`).
"""

from __future__ import annotations

import io
import shutil
import time
import tracemalloc
import wave

import pytest
from fastapi.testclient import TestClient

from conftest import KOKORO_MODEL, KOKORO_VOICES, PIPER_MODEL, kokoro_ready, piper_ready
from opal_talk.api.app import create_app
from opal_talk.config import ServiceConfig

needs_piper = pytest.mark.skipif(not piper_ready(), reason=f"Piper runtime or model {PIPER_MODEL} not available")
needs_kokoro = pytest.mark.skipif(not kokoro_ready(), reason="kokoro-onnx runtime or model files not available")

TEXT = "Hello from OPAL Talk release validation. A second sentence."


def _timed_stream(client, body):
    start = time.monotonic()
    arrivals, total = [], 0
    with client.stream("POST", "/api/v1/generate/stream", json=body) as resp:
        status = resp.status_code
        headers = dict(resp.headers)
        for chunk in resp.iter_raw():
            arrivals.append(time.monotonic() - start)
            total += len(chunk)
    return status, headers, arrivals, total


def _measure(client, voice_id):
    """One full generate round-trip with timings and Python allocation peak."""
    tracemalloc.start()
    status, headers, arrivals, nbytes = _timed_stream(
        client, {"text": TEXT, "voice_id": voice_id})
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return status, headers, arrivals, nbytes, peak


# -- Piper -----------------------------------------------------------------------


@needs_piper
@pytest.mark.piper
class TestPiperRelease:
    @pytest.fixture
    def app(self, tmp_path):
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        for suffix in ("", ".json"):
            shutil.copy(f"{PIPER_MODEL}{suffix}", voices_dir / f"test-voice.onnx{suffix}")
        cfg = ServiceConfig(
            data_dir=tmp_path / "var", config_dir=tmp_path / "config",
            engines={"piper": {"options": {"voices_dir": str(voices_dir), "cache_size": 2}},
                       "kokoro": {"enabled": False}},
        )
        app = create_app(cfg)
        yield app
        app.state.services.close()

    @pytest.fixture
    def client(self, app):
        with TestClient(app) as c:
            resp = c.post("/api/v1/voices/", json={
                "voice_id": "p", "name": "P", "engine_id": "piper", "engine_voice_ref": "test-voice"})
            assert resp.status_code == 201, resp.text
            yield c

    def test_engine_available_and_model_loads(self, client):
        body = client.get("/api/v1/engines/piper").json()
        assert body["available"] and body["enabled"]

    def test_wav_generate_format(self, client):
        native = client.get("/api/v1/voices/p").json()["native_sample_rate"]
        resp = client.post("/api/v1/generate", json={"text": TEXT, "voice_id": "p"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.headers["x-opal-sample-rate"] == str(native)
        with wave.open(io.BytesIO(resp.content)) as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == native
            assert w.getnframes() > native * 0.5

    def test_pcm_stream_headers_and_audio(self, client):
        status, headers, arrivals, nbytes = _timed_stream(
            client, {"text": TEXT, "voice_id": "p", "sample_rate": 16000})
        assert status == 200 and nbytes > 0
        assert headers["x-opal-sample-rate"] == "16000"
        assert headers["x-opal-channels"] == "1"
        assert headers["x-opal-sample-format"] == "s16le"
        assert len(arrivals) > 0

    def test_repeated_generation_uses_model_cache(self, app, client):
        engine = app.state.services.runtime.get("piper")
        for _ in range(3):
            resp = client.post("/api/v1/generate",
                               json={"text": "Caching check.", "voice_id": "p", "output_format": "pcm"})
            assert resp.status_code == 200
        # One voice loaded once and held in the model cache across requests.
        assert len(engine._models._entries) == 1
        assert next(iter(engine._models._entries.values())).refs == 0

    def test_safe_config_update_between_requests(self, client):
        resp = client.patch("/api/v1/engines/piper",
                            json={"options": {"cache_size": 1}},
                            headers={"If-Match": client.get("/api/v1/settings/").headers["etag"]})
        assert resp.status_code == 200, resp.text
        for _ in range(2):  # requests after the update still succeed
            resp = client.post("/api/v1/generate",
                               json={"text": "Post update.", "voice_id": "p", "output_format": "pcm"})
            assert resp.status_code == 200 and len(resp.content) > 0

    def test_performance_measurements(self, client):
        status, _, arrivals, nbytes, peak = _measure(client, "p")
        assert status == 200 and nbytes > 0 and arrivals
        # Report only — thresholds are hardware-dependent and intentionally absent.
        print(f"\npiper release: ttfb={arrivals[0]:.3f}s total={arrivals[-1]:.3f}s "
              f"bytes={nbytes} pyalloc_peak={peak}")


# -- Kokoro ----------------------------------------------------------------------


@needs_kokoro
@pytest.mark.kokoro
class TestKokoroRelease:
    @pytest.fixture
    def app(self, tmp_path):
        cfg = ServiceConfig(
            data_dir=tmp_path / "var", config_dir=tmp_path / "config",
            engines={"kokoro": {"options": {"model_path": str(KOKORO_MODEL),
                                            "voices_path": str(KOKORO_VOICES)}}},
        )
        app = create_app(cfg)
        yield app
        app.state.services.close()

    @pytest.fixture
    def client(self, app):
        with TestClient(app) as c:
            resp = c.post("/api/v1/voices/", json={
                "voice_id": "k", "name": "K", "engine_id": "kokoro", "engine_voice_ref": "af_heart"})
            assert resp.status_code == 201, resp.text
            yield c

    def test_engine_available_and_model_loads(self, client):
        body = client.get("/api/v1/engines/kokoro").json()
        assert body["available"] and body["enabled"]

    def test_wav_generate_format(self, client):
        resp = client.post("/api/v1/generate", json={"text": TEXT, "voice_id": "k"})
        assert resp.status_code == 200 and resp.headers["x-opal-sample-rate"] == "24000"
        with wave.open(io.BytesIO(resp.content)) as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 24000
            assert w.getnframes() > 24000 * 0.5

    def test_pcm_stream_headers_and_audio(self, client):
        status, headers, arrivals, nbytes = _timed_stream(
            client, {"text": TEXT, "voice_id": "k", "sample_rate": 16000})
        assert status == 200 and nbytes > 0
        assert headers["x-opal-sample-rate"] == "16000"
        assert headers["x-opal-channels"] == "1"
        assert headers["x-opal-sample-format"] == "s16le"
        _ = arrivals

    def test_safe_config_update_between_requests(self, client):
        resp = client.patch("/api/v1/engines/kokoro",
                            json={"options": {"default_lang": "en-gb"}},
                            headers={"If-Match": client.get("/api/v1/settings/").headers["etag"]})
        assert resp.status_code == 200, resp.text
        resp = client.post("/api/v1/generate",
                           json={"text": "Post update.", "voice_id": "k", "output_format": "pcm"})
        assert resp.status_code == 200 and len(resp.content) > 0

    def test_performance_measurements(self, client):
        status, _, arrivals, nbytes, peak = _measure(client, "k")
        assert status == 200 and nbytes > 0 and arrivals
        print(f"\nkokoro release: ttfb={arrivals[0]:.3f}s total={arrivals[-1]:.3f}s "
              f"bytes={nbytes} pyalloc_peak={peak}")
