"""Performance and acceptance coverage for speech generation.

The deterministic tests measure the FakeEngine path and run in normal CI; the real
Piper and Kokoro smoke tests are skipped unless their runtimes and model files are
installed (see docs/PERFORMANCE_TESTS.md). Timing assertions are relative — work
ratios, not absolute seconds — so they hold on slow shared runners and fast
development machines alike.
"""

from __future__ import annotations

import shutil
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import (
    KOKORO_MODEL,
    KOKORO_VOICES,
    PIPER_MODEL,
    kokoro_ready,
    make_voice,
    piper_ready,
)
from opal_talk.api.app import create_app
from opal_talk.config import ServiceConfig
from opal_talk.schemas import GenerateStreamRequest
from opal_talk.tts.base import AudioChunk
from opal_talk.tts.chunking import chunk_text

SENTENCES = " ".join(f"This is sentence number {i}." for i in range(6))
STREAM_PATH = "/api/v1/generate/stream"

needs_piper = pytest.mark.skipif(not piper_ready(), reason=f"Piper runtime or model {PIPER_MODEL} not available")
needs_kokoro = pytest.mark.skipif(not kokoro_ready(), reason="kokoro-onnx runtime or model files not available")


@pytest.fixture
def voices(client):
    make_voice(client, "v1", ref="tone-a")
    make_voice(client, "v2", ref="tone-b")
    client.put("/api/v1/settings/chunk_max_chars", json={"value": "50"})


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _stream(url: str, body: dict, timeout: float = 60) -> tuple[int, list[float], int]:
    """Stream one request over real HTTP; return (status, arrival times, byte count)."""
    start = time.monotonic()
    arrivals: list[float] = []
    nbytes = 0
    with httpx.stream("POST", url, json=body, timeout=timeout) as resp:
        status = resp.status_code
        for chunk in resp.iter_raw():
            arrivals.append(time.monotonic() - start)
            nbytes += len(chunk)
    return status, arrivals, nbytes


# -- time to first byte ---------------------------------------------------------------


def test_time_to_first_byte_is_progressive(live_server, client, voices):
    """TTFB tracks one engine call, not the total length of the text."""
    delay = {"engine_options": {"delay": 0.25}, "voice_id": "v1"}
    status, short, _ = _stream(f"{live_server}{STREAM_PATH}", {"text": SENTENCES, **delay})
    assert status == 200
    status, long, _ = _stream(f"{live_server}{STREAM_PATH}", {"text": SENTENCES * 4, **delay})
    assert status == 200
    # Both streams are progressive: the first block precedes the midpoint of delivery.
    assert short[0] < short[-1] / 2
    assert long[0] < long[-1] / 3
    # The long request produces ~4x the audio but its TTFB does not grow with it.
    assert long[-1] > short[-1] * 2
    assert long[0] < max(short[0] * 3, 1.0)


# -- total synthesis duration ----------------------------------------------------------


def test_synthesis_duration_tracks_engine_latency(live_server, client, voices):
    """End-to-end duration follows per-chunk engine latency, not a fixed overhead."""
    body = {"text": SENTENCES, "voice_id": "v1"}
    _, fast, _ = _stream(f"{live_server}{STREAM_PATH}", {**body, "engine_options": {"delay": 0.05}})
    _, slow, _ = _stream(f"{live_server}{STREAM_PATH}", {**body, "engine_options": {"delay": 0.3}})
    assert slow[-1] > fast[-1] * 2


def test_streaming_delivery_is_steady(live_server, client, voices):
    """Blocks arrive spread across the whole duration (no buffer-then-dump)."""
    body = {"text": SENTENCES, "voice_id": "v1", "engine_options": {"delay": 0.2}}
    status, arrivals, nbytes = _stream(f"{live_server}{STREAM_PATH}", body)
    assert status == 200 and len(arrivals) >= 3
    total = arrivals[-1]
    assert arrivals[0] < total * 0.5  # production starts immediately
    assert arrivals[-1] - arrivals[0] > total * 0.4  # delivery occupies the duration
    assert nbytes > 0


# -- intermediate buffering -------------------------------------------------------------


def test_stream_peak_memory_stays_below_output_size(client, voices, app):
    """Lazy iteration never materializes the completed PCM: allocation peak stays
    far below the total size of the stream."""
    svc = app.state.services.generation
    text = SENTENCES * 40  # ~2 MiB of PCM, many engine chunks
    plan = svc.plan(GenerateStreamRequest(text=text, voice_id="v1"))
    tracemalloc.start()
    total = 0
    for block in svc.pcm_stream(plan):
        total += len(block)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert total > 1 << 20
    assert peak < total // 2


# -- chunk ordering and output correctness ------------------------------------------------


def test_chunk_ordering_is_preserved(client, voices, engines):
    """Each streamed block carries its text chunk's marker amplitude, in order."""
    engine = engines[0]
    rate = engine.VOICES["tone-a"]

    def numbered(ref, text, speed, options):
        engine.calls.append({"ref": ref, "text": text, "speed": speed, "options": dict(options)})
        n = max(1, int(rate * 0.01 * len(text) / speed))
        yield AudioChunk(np.full(n, 0.01 * len(engine.calls), dtype=np.float32), rate)

    engine.synthesize = numbered
    with client.stream("POST", STREAM_PATH,
                       json={"text": SENTENCES, "voice_id": "v1", "sample_rate": rate}) as resp:
        assert resp.status_code == 200
        pcm = resp.read()
    texts = [c["text"] for c in engine.calls]
    assert len(texts) > 1 and texts == chunk_text(SENTENCES, 50)
    sizes = [max(1, int(rate * 0.01 * len(t))) for t in texts]
    assert len(pcm) == 2 * sum(sizes)
    samples = np.frombuffer(pcm, "<i2")
    medians, offset = [], 0
    for n in sizes:
        medians.append(int(np.median(samples[offset:offset + n])))
        offset += n
    # Block i carries marker 0.01 * (i + 1): strictly increasing, none duplicated.
    assert len(set(medians)) == len(medians) == len(sizes)
    assert medians == sorted(medians)


def test_stream_matches_buffered_render(client, voices):
    """Streaming output is byte-identical to the completed /generate PCM."""
    body = {"text": SENTENCES, "voice_id": "v1", "volume": 0.7, "sample_rate": 16000}
    with client.stream("POST", STREAM_PATH, json=body) as resp:
        streamed = resp.read()
    completed = client.post("/api/v1/generate", json={**body, "output_format": "pcm"}).content
    assert streamed == completed


# -- long text ----------------------------------------------------------------------------


def test_long_text_generation(client, voices, engines, app):
    """A long input is chunked, ordered and complete end to end."""
    text = " ".join(f"Long-form sentence number {i} for streaming." for i in range(80))
    expected = chunk_text(text, 50)
    assert len(expected) > 30
    with client.stream("POST", STREAM_PATH, json={"text": text, "voice_id": "v1"}) as resp:
        assert resp.status_code == 200
        pcm = resp.read()
    calls = [c["text"] for c in engines[0].calls]
    assert calls == expected
    assert len(pcm) == 2 * sum(max(1, int(22050 * 0.01 * len(t))) for t in expected)
    rec = app.state.services.runtime._records["fake"]
    assert _wait_until(lambda: rec.refs == 0)


# -- bounded inference concurrency ----------------------------------------------------------


def test_concurrent_streams_respect_engine_capacity(live_server, client, voices, engines):
    """Parallel streams overlap up to — and never beyond — the configured permit count."""
    client.patch("/api/v1/engines/fake", json={"options": {"max_concurrent_inferences": 2}})
    engine = engines[0]
    synthesize = engine.synthesize
    lock = threading.Lock()
    inflight = {"n": 0, "peak": 0}

    def tracked(ref, text, speed, options):
        with lock:
            inflight["n"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["n"])
        try:
            yield from synthesize(ref, text, speed, options)
        finally:
            with lock:
                inflight["n"] -= 1

    engine.synthesize = tracked
    body = {"text": SENTENCES, "voice_id": "v1", "engine_options": {"delay": 0.15}}

    def stream_once(_i: int) -> bool:
        status, _, nbytes = _stream(f"{live_server}{STREAM_PATH}", body)
        return status == 200 and nbytes > 0

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert all(pool.map(stream_once, range(3)))
    assert 1 < inflight["peak"] <= 2


# -- real-engine smoke tests (skipped unless runtimes + models are installed) ---------------


def _smoke_stream(client, voice_id: str) -> tuple[int, dict, list[float], bytes]:
    start = time.monotonic()
    arrivals: list[float] = []
    body = bytearray()
    with client.stream("POST", STREAM_PATH,
                       json={"text": "Hello from OPAL Talk. A second sentence.", "voice_id": voice_id}) as resp:
        status = resp.status_code
        headers = dict(resp.headers)
        for chunk in resp.iter_raw():
            arrivals.append(time.monotonic() - start)
            body += chunk
    return status, headers, arrivals, bytes(body)


@needs_piper
@pytest.mark.piper
def test_piper_stream_smoke(tmp_path):
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    for suffix in ("", ".json"):
        shutil.copy(f"{PIPER_MODEL}{suffix}", voices_dir / f"test-voice.onnx{suffix}")
    cfg = ServiceConfig(
        data_dir=tmp_path / "var", config_dir=tmp_path / "config",
        engines={"piper": {"options": {"voices_dir": str(voices_dir)}}, "kokoro": {"enabled": False}},
    )
    app = create_app(cfg)
    try:
        with TestClient(app) as c:
            resp = c.post("/api/v1/voices/", json={"voice_id": "p", "name": "P", "engine_id": "piper",
                                                    "engine_voice_ref": "test-voice"})
            assert resp.status_code == 201, resp.text
            status, headers, arrivals, pcm = _smoke_stream(c, "p")
            assert status == 200 and len(pcm) > 0
            assert int(headers["x-opal-sample-rate"]) > 0
            samples = np.frombuffer(pcm, "<i2").astype(np.int32)
            assert np.abs(samples).max() > 1000  # audible output, not silence
            print(f"\npiper stream: ttfb={arrivals[0]:.3f}s total={arrivals[-1]:.3f}s "
                  f"bytes={len(pcm)} blocks={len(arrivals)}")
    finally:
        app.state.services.close()


@needs_kokoro
@pytest.mark.kokoro
def test_kokoro_stream_smoke(tmp_path):
    cfg = ServiceConfig(
        data_dir=tmp_path / "var", config_dir=tmp_path / "config",
        engines={"kokoro": {"options": {"model_path": str(KOKORO_MODEL), "voices_path": str(KOKORO_VOICES)}}},
    )
    app = create_app(cfg)
    try:
        with TestClient(app) as c:
            resp = c.post("/api/v1/voices/", json={"voice_id": "k", "name": "K", "engine_id": "kokoro",
                                                    "engine_voice_ref": "af_heart"})
            assert resp.status_code == 201, resp.text
            status, headers, arrivals, pcm = _smoke_stream(c, "k")
            assert status == 200 and len(pcm) > 0
            assert headers["x-opal-sample-rate"] == "24000"
            samples = np.frombuffer(pcm, "<i2").astype(np.int32)
            assert np.abs(samples).max() > 1000
            print(f"\nkokoro stream: ttfb={arrivals[0]:.3f}s total={arrivals[-1]:.3f}s "
                  f"bytes={len(pcm)} blocks={len(arrivals)}")
    finally:
        app.state.services.close()
