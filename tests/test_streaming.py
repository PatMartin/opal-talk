"""Streaming PCM: headers, raw byte format, progressive delivery, constant rate, and failure semantics."""

from __future__ import annotations

import math
import sys
import time

import httpx
import pytest
from contract_check import check_response

from conftest import make_profile, make_voice

SENTENCES = " ".join(f"This is sentence number {i}." for i in range(6))


def _runtime_refs(app, engine_id: str = "fake") -> int:
    return app.state.services.runtime._records[engine_id].refs


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def voices(client):
    make_voice(client, "v1", ref="tone-a")
    make_voice(client, "v2", ref="tone-b")
    client.put("/api/v1/settings/chunk_max_chars", json={"value": "50"})


def test_stream_headers_and_raw_pcm(client, voices, engines):
    p = make_profile(client, "p", "v1", is_active=True, output_format="wav")
    with client.stream("POST", "/api/v1/generate/stream", json={"text": SENTENCES}) as resp:
        body = resp.read()
        check_response(resp, "POST", "/api/v1/generate/stream")
    h = resp.headers
    assert resp.status_code == 200
    assert h["content-type"] == "application/octet-stream"
    assert (h["x-opal-audio-format"], h["x-opal-channels"], h["x-opal-sample-format"]) == ("pcm", "1", "s16le")
    assert h["x-opal-sample-rate"] == "22050" and h["x-opal-profile-id"] == str(p["id"])
    assert body[:4] != b"RIFF" and len(body) % 2 == 0
    texts = [c["text"] for c in engines[0].calls]
    assert len(texts) > 1 and " ".join(texts) == SENTENCES
    assert len(body) == sum(2 * int(22050 * 0.01 * len(t)) for t in texts)


def test_stream_matches_completed_pcm(client, voices):
    body = {"text": SENTENCES, "voice_id": "v2", "volume": 0.7, "sample_rate": 16000}
    with client.stream("POST", "/api/v1/generate/stream", json=body) as resp:
        streamed = resp.read()
    completed = client.post("/api/v1/generate", json={**body, "output_format": "pcm"}).content
    assert streamed == completed


def test_sample_rate_constant_across_chunks(client, voices, engines):
    with client.stream("POST", "/api/v1/generate/stream",
                       json={"text": SENTENCES, "voice_id": "v1", "sample_rate": 8000}) as resp:
        data = resp.read()
    assert resp.headers["x-opal-sample-rate"] == "8000"
    texts = [c["text"] for c in engines[0].calls]
    assert len(texts) > 1
    # Every internal chunk (native 22050 Hz) is resampled to the single advertised rate.
    expected = sum(math.ceil(int(22050 * 0.01 * len(t)) * 8000 / 22050) for t in texts)
    assert len(data) == 2 * expected


def test_stream_rejects_output_format(client, voices):
    resp = client.post("/api/v1/generate/stream", json={"text": "x", "voice_id": "v1", "output_format": "wav"})
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "body,status",
    [
        ({"text": "x"}, 409),
        ({"text": "x", "voice_id": "v1", "engine_options": {"nope": 1}}, 422),
        ({"text": "x", "voice_id": "v1", "engine_options": {"fail_on_call": 1}}, 500),
        ({"text": "x", "voice_id": "v1", "engine_options": {"pitch": 999, "strict": True}}, 422),
    ],
)
def test_failures_before_streaming_are_json(client, voices, body, status):
    resp = client.post("/api/v1/generate/stream", json=body)
    assert resp.status_code == status
    assert resp.headers["content-type"] == "application/json"


def test_too_large_and_unavailable(client, voices):
    client.put("/api/v1/settings/max_text_chars", json={"value": "5"})
    assert client.post("/api/v1/generate/stream", json={"text": "123456", "voice_id": "v1"}).status_code == 413
    client.delete("/api/v1/settings/max_text_chars")
    client.patch("/api/v1/engines/fake", json={"options": {"available": False}})
    assert client.post("/api/v1/generate/stream", json={"text": "x", "voice_id": "v1"}).status_code == 503


def test_progressive_delivery_over_real_http(live_server, client, voices):
    body = {"text": SENTENCES, "voice_id": "v1", "engine_options": {"delay": 0.3}}
    start = time.monotonic()
    arrivals: list[float] = []
    with httpx.stream("POST", f"{live_server}/api/v1/generate/stream", json=body, timeout=30) as resp:
        assert resp.status_code == 200
        for _ in resp.iter_raw():
            arrivals.append(time.monotonic() - start)
    total = arrivals[-1]
    assert len(arrivals) > 1
    assert arrivals[0] < total / 2, f"first audio at {arrivals[0]:.2f}s of {total:.2f}s; not progressive"


def test_failure_after_start_terminates_stream(live_server, client, voices):
    body = {"text": SENTENCES, "voice_id": "v1", "engine_options": {"fail_on_call": 3}}
    received = 0
    with (
        pytest.raises(httpx.HTTPError),
        httpx.stream("POST", f"{live_server}/api/v1/generate/stream", json=body, timeout=30) as resp,
    ):
        assert resp.status_code == 200
        for chunk in resp.iter_raw():
            received += len(chunk)
    assert received > 0


# -- stream failure handling and cleanup --------------------------------------------


def test_failure_before_first_block_is_json_and_releases(client, voices, app):
    """A synthesis failure before 200 is a JSON error and frees the engine binding."""
    body = {"text": "x", "voice_id": "v1", "engine_options": {"fail_on_call": 1}}
    resp = client.post("/api/v1/generate/stream", json=body)
    assert resp.status_code == 500
    assert resp.headers["content-type"] == "application/json"
    assert resp.json()["error"]["code"] == "generation_failed"
    assert _wait_until(lambda: _runtime_refs(app) == 0)


def test_failure_after_first_block_terminates_and_logs(client, voices, app, caplog):
    """A synthesis failure after 200 terminates the stream (contract) and is logged
    with the request's correlation identifiers."""
    body = {"text": SENTENCES, "voice_id": "v1", "engine_options": {"fail_on_call": 2}}
    with client.stream("POST", "/api/v1/generate/stream",
                       json=body, headers={"X-Opal-Request-Id": "req-stream-fail"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["x-opal-request-id"] == "req-stream-fail"
        it = resp.iter_raw()
        assert next(it)  # the first block arrived intact
        # Premature termination: the stream is torn down rather than receiving an
        # error object injected into the byte stream.
        with pytest.raises(RuntimeError, match="response already started"):
            next(it)
    assert _wait_until(lambda: _runtime_refs(app) == 0)
    logged = [r.getMessage() for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
    assert any("synthesis failure" in m and "req-stream-fail" in m for m in logged), logged


def test_early_close_releases_generator_and_permit(client, voices, app):
    """Abandoning a live response frees the runtime reference and inference permit."""
    rec = app.state.services.runtime._records["fake"]
    body = {"text": SENTENCES, "voice_id": "v1", "engine_options": {"delay": 0.02}}
    with client.stream("POST", "/api/v1/generate/stream", json=body) as resp:
        assert resp.status_code == 200
        assert next(resp.iter_raw())
        assert rec.refs == 1
    assert _wait_until(lambda: rec.refs == 0)
    # The released permit lets the next request run normally.
    assert client.post("/api/v1/generate", json={"text": "ok", "voice_id": "v1"}).status_code == 200


def test_close_tolerates_engine_teardown_failure(client, voices, engines, app, monkeypatch):
    """A teardown error inside the engine generator is reported, not fatal to cleanup."""
    unraisable: list[BaseException] = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: unraisable.append(u.exc_value))
    engine = engines[0]
    synthesize = engine.synthesize

    def flaky(ref, text, speed, options):
        try:
            yield from synthesize(ref, text, speed, options)
        except GeneratorExit:
            raise RuntimeError("engine teardown failed")

    engine.synthesize = flaky
    rec = app.state.services.runtime._records["fake"]
    with client.stream("POST", "/api/v1/generate/stream", json={"text": SENTENCES, "voice_id": "v1"}) as resp:
        assert next(resp.iter_raw())
    assert _wait_until(lambda: rec.refs == 0)
    assert any(isinstance(e, RuntimeError) and "teardown" in str(e) for e in unraisable)


def test_completed_stream_does_not_log_failure(client, voices, caplog):
    """Normal end-of-stream is distinguished from failures: no warning is logged."""
    with client.stream("POST", "/api/v1/generate/stream", json={"text": SENTENCES, "voice_id": "v1"}) as resp:
        assert resp.status_code == 200
        resp.read()
    logged = [r.getMessage() for r in caplog.records]
    assert not any("synthesis failure" in m or "closed before completion" in m for m in logged)
