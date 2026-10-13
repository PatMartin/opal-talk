"""Generation: settings resolution, output formats, engine routing and error paths."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from conftest import make_profile, make_voice


@pytest.fixture
def setup(client, engines):
    make_voice(client, "v1", ref="tone-a", engine_options={"pitch": 300})
    make_voice(client, "v2", ref="tone-b")
    make_voice(client, "va", engine_id="alt", ref="tone-a")
    return engines[0]


def test_wav_with_active_profile(client, setup):
    p = make_profile(client, "p", "v1", is_active=True, engine_options={"pitch": 400})
    resp = client.post("/api/v1/generate", json={"text": "Hello world."})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["x-opal-profile-id"] == str(p["id"])
    assert resp.headers["x-opal-voice-id"] == "v1"
    assert resp.headers["x-opal-engine-id"] == "fake"
    assert resp.headers["x-opal-sample-rate"] == "22050"
    with wave.open(io.BytesIO(resp.content)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 22050)
        assert w.getnframes() == int(22050 * 0.01 * len("Hello world."))
    assert setup.calls[-1]["options"] == {"pitch": 400}


def test_pcm_output(client, setup):
    resp = client.post("/api/v1/generate", json={"text": "abc", "voice_id": "v2", "output_format": "pcm"})
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["x-opal-audio-format"] == "pcm"
    assert "x-opal-profile-id" not in resp.headers
    assert len(resp.content) == 2 * int(16000 * 0.01 * 3)


def test_request_overrides_profile_and_merges_engine_options(client, setup):
    p = make_profile(client, "p", "v1", speed=2.0, volume=0.5, output_format="pcm", sample_rate=8000,
                     engine_options={"pitch": 500, "strict": False})
    resp = client.post("/api/v1/generate", json={
        "text": "Hello.", "profile_id": p["id"], "speed": 1.0, "engine_options": {"pitch": 600},
    })
    assert resp.headers["x-opal-sample-rate"] == "8000"
    assert resp.headers["x-opal-audio-format"] == "pcm"
    call = setup.calls[-1]
    assert call["speed"] == 1.0
    assert call["options"] == {"pitch": 600, "strict": False}
    peak = np.abs(np.frombuffer(resp.content, "<i2")).max()
    # Volume 0.5 on a 0.5-amplitude tone; small overshoot allowed from resampling to 8 kHz.
    assert 0.2 * 32767 < peak <= 0.26 * 32767


def test_explicit_profile_beats_active_profile(client, setup):
    make_profile(client, "active", "v1", is_active=True)
    other = make_profile(client, "other", "v2")
    resp = client.post("/api/v1/generate", json={"text": "x", "profile_id": other["id"]})
    assert resp.headers["x-opal-voice-id"] == "v2"
    assert resp.headers["x-opal-profile-id"] == str(other["id"])


def test_service_defaults_from_settings(client, setup):
    client.put("/api/v1/settings/default_output_format", json={"value": "pcm"})
    client.put("/api/v1/settings/default_speed", json={"value": "2.0"})
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "v1"})
    assert resp.headers["x-opal-audio-format"] == "pcm"
    assert setup.calls[-1]["speed"] == 2.0


def test_profile_options_not_applied_to_other_engine(client, setup, engines):
    make_profile(client, "p", "v1", is_active=True, engine_options={"pitch": 500})
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "va"})
    assert resp.status_code == 200
    assert engines[1].calls[-1]["options"] == {}


def test_requested_sample_rate_is_honoured(client, setup):
    for rate in (8000, 44100, 48000):
        resp = client.post("/api/v1/generate", json={"text": "Hello", "voice_id": "v1", "sample_rate": rate})
        with wave.open(io.BytesIO(resp.content)) as w:
            assert w.getframerate() == rate
        assert resp.headers["x-opal-sample-rate"] == str(rate)


def test_long_text_is_chunked_in_order(client, setup):
    client.put("/api/v1/settings/chunk_max_chars", json={"value": "50"})
    text = " ".join(f"Sentence number {i}." for i in range(20))
    resp = client.post("/api/v1/generate", json={"text": text, "voice_id": "v1", "output_format": "pcm"})
    assert resp.status_code == 200
    texts = [c["text"] for c in setup.calls]
    assert len(texts) > 1 and all(len(t) <= 50 for t in texts)
    assert " ".join(texts) == text
    assert len(resp.content) == sum(2 * int(22050 * 0.01 * len(t)) for t in texts)


def test_engine_override(client, setup, engines):
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "v1", "engine_id": "alt"})
    assert resp.status_code == 200
    assert resp.headers["x-opal-engine-id"] == "alt" and resp.headers["x-opal-sample-rate"] == "24000"
    assert engines[1].calls[-1]["options"] == {"pitch": 300}
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "v1", "engine_id": "fake"})
    assert resp.status_code == 200 and resp.headers["x-opal-engine-id"] == "fake"


def test_engine_override_mismatch(client, setup):
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "v2", "engine_id": "alt"})
    assert resp.status_code == 409
    assert resp.json() == {
        "code": "voice_engine_mismatch",
        "message": "The requested engine cannot use the resolved voice.",
        "details": {"voice_id": "v2", "requested_engine_id": "alt"},
    }
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "v1", "engine_id": "ghost"})
    assert resp.status_code == 409 and resp.json()["code"] == "engine_not_found"


@pytest.mark.parametrize(
    "body,status,code",
    [
        ({"text": "x"}, 409, "no_voice"),
        ({"text": "x", "voice_id": "ghost"}, 409, "voice_not_found"),
        ({"text": "x", "profile_id": 999}, 409, "profile_not_found"),
        ({"text": "x", "voice_id": "va", "speed": 3.0}, 409, "unsupported_speed"),
    ],
)
def test_conflicts(client, setup, body, status, code):
    resp = client.post("/api/v1/generate", json=body)
    assert resp.status_code == status and resp.json()["code"] == code


def test_disabled_voice_and_engine(client, setup):
    client.patch("/api/v1/voices/v2", json={"enabled": False})
    assert client.post("/api/v1/generate", json={"text": "x", "voice_id": "v2"}).json()["code"] == "voice_disabled"
    client.patch("/api/v1/engines/fake", json={"enabled": False})
    assert client.post("/api/v1/generate", json={"text": "x", "voice_id": "v1"}).json()["code"] == "engine_disabled"


def test_unavailable_engine_is_503(client, setup):
    client.patch("/api/v1/engines/fake", json={"options": {"available": False}})
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "v1"})
    assert resp.status_code == 503 and resp.json()["code"] == "engine_unavailable"


def test_payload_too_large(client, setup):
    client.put("/api/v1/settings/max_text_chars", json={"value": "10"})
    resp = client.post("/api/v1/generate", json={"text": "x" * 11, "voice_id": "v1"})
    assert resp.status_code == 413 and resp.json()["details"]["max_text_chars"] == 10


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": ""},
        {"text": "   "},
        {"text": "x", "voice_id": "v1", "speed": 0.1},
        {"text": "x", "voice_id": "v1", "volume": 2.5},
        {"text": "x", "voice_id": "v1", "sample_rate": 4000},
        {"text": "x", "voice_id": "v1", "output_format": "mp3"},
        {"text": "x", "voice_id": "v1", "ssml": True},
        {"text": "x", "voice_id": "v1", "engine_options": {"unknown": 1}},
        {"text": "x", "voice_id": "v1", "engine_options": {"model_dir": "/voice-scope-only"}},
        {"text": "x", "voice_id": "v1", "engine_options": {"pitch": 5}},
        {"text": "x", "voice_id": "v1", "engine_options": {"pitch": 999, "strict": True}},
        {"text": "x", "profile_id": 0},
    ],
)
def test_validation_errors(client, setup, body):
    resp = client.post("/api/v1/generate", json=body)
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], list)


def test_generation_failure_is_500(client, setup):
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "v1", "engine_options": {"fail_on_call": 1}})
    assert resp.status_code == 500 and resp.json()["code"] == "generation_failed"


def test_voice_test_endpoint(client, setup):
    resp = client.post("/api/v1/voices/v2/test")
    assert resp.status_code == 200 and resp.headers["content-type"] == "audio/wav"
    assert setup.calls[-1]["text"] == "This is a test of the selected OPAL Talk voice."
    resp = client.post("/api/v1/voices/v1/test", json={"text": "Hi", "output_format": "pcm", "sample_rate": 16000,
                                                       "speed": 2.0, "engine_options": {"pitch": 900}})
    assert resp.headers["x-opal-sample-rate"] == "16000"
    assert setup.calls[-1]["options"] == {"pitch": 900} and setup.calls[-1]["speed"] == 2.0
    assert client.post("/api/v1/voices/ghost/test").status_code == 404
    assert client.post("/api/v1/voices/v1/test", json={"text": ""}).status_code == 422
    client.patch("/api/v1/voices/v1", json={"enabled": False})
    assert client.post("/api/v1/voices/v1/test").status_code == 409
    client.patch("/api/v1/engines/fake", json={"options": {"available": False}})
    assert client.post("/api/v1/voices/v2/test").status_code == 503
