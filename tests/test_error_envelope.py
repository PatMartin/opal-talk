"""OpalError envelope: every error status serializes as {"error": {...}} with correlation IDs."""

from __future__ import annotations

import re

from httpx import Response

from conftest import make_profile, make_voice

IDS = {"X-Opal-Session-Id": "sess-e1", "X-Opal-Causation-Id": "cause-e1"}


def assert_opal_error(resp: Response, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    err = resp.json()["error"]
    assert err["code"] == code
    assert isinstance(err["message"], str) and err["message"]
    # Body identifiers match the stamped response headers.
    assert err["request_id"] == resp.headers["X-Opal-Request-Id"]
    assert err["transaction_id"] == resp.headers["X-Opal-Transaction-Id"]
    assert re.fullmatch(r"[!-~]{1,128}", err["request_id"])
    assert re.fullmatch(r"[!-~]{1,128}", err["transaction_id"])
    return err


def test_not_found_envelope(client):
    err = assert_opal_error(client.get("/api/v1/engines/nope"), 404, "engine_not_found")
    assert "session_id" not in err and "causation_id" not in err


def test_not_found_echoes_context_ids(client):
    resp = client.get("/api/v1/engines/nope", headers=IDS)
    err = assert_opal_error(resp, 404, "engine_not_found")
    assert err["session_id"] == "sess-e1" and err["causation_id"] == "cause-e1"


def test_conflict_envelope_preserves_diagnostic_details(client):
    make_voice(client, "v1")
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "ghost"})
    err = assert_opal_error(resp, 409, "voice_not_found")
    assert err["details"]["voice_id"] == "ghost"


def test_request_validation_errors_land_in_details_errors(client):
    resp = client.post("/api/v1/voices/", json={})
    err = assert_opal_error(resp, 422, "unprocessable_entity")
    locs = {tuple(e["loc"]) for e in err["details"]["errors"]}
    assert ("body", "voice_id") in locs
    for e in err["details"]["errors"]:
        assert set(e) >= {"loc", "msg", "type"}


def test_invalid_correlation_header_is_an_opal_error(client):
    client.skip_contract = True  # the v1.0 contract does not document this 422
    resp = client.get("/healthz", headers={"X-Opal-Transaction-Id": "bad id"})
    err = assert_opal_error(resp, 422, "unprocessable_entity")
    assert err["details"]["errors"][0]["loc"] == ["header", "X-Opal-Transaction-Id"]


def test_engine_unavailable_is_503(client):
    make_voice(client, "v1")
    client.patch("/api/v1/engines/fake", json={"options": {"available": False}})
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "v1"})
    assert_opal_error(resp, 503, "engine_unavailable")


def test_internal_error_is_500(client):
    make_voice(client, "v1")
    resp = client.post("/api/v1/generate", json={"text": "x", "voice_id": "v1", "engine_options": {"fail_on_call": 1}})
    assert_opal_error(resp, 500, "generation_failed")


def test_invalid_request_uses_same_envelope(client):
    """Semantic InvalidRequest failures (not Pydantic) land in details.errors too."""
    make_voice(client, "v1")
    resp = client.post(
        "/api/v1/generate",
        json={"text": "x", "voice_id": "v1", "engine_options": {"pitch": 999, "strict": True}},
    )
    err = assert_opal_error(resp, 422, "unprocessable_entity")
    assert any("pitch" in str(e["loc"]) for e in err["details"]["errors"])


def test_generated_ids_appear_when_headers_absent(client):
    resp = client.get("/api/v1/engines/nope")
    err = resp.json()["error"]
    assert err["request_id"] == resp.headers["X-Opal-Request-Id"]
    assert err["transaction_id"] == resp.headers["X-Opal-Transaction-Id"]
