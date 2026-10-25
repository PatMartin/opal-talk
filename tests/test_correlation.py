"""Correlation middleware: X-Opal-* identifier validation, propagation and echo.

Covers the contract-1.2.0 context convention on the v1.0.0 runtime: the extra
response headers are contract-compatible (undocumented headers are ignored by the
v1 checks), while deliberately invalid identifiers are exercised with
`skip_contract` because the active contract does not document those 422s.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from conftest import make_voice
from opal_talk.api.correlation import CORRELATION_HEADERS, CorrelationMiddleware, correlation_ids

IDS = {
    "X-Opal-Session-Id": "sess-abc",
    "X-Opal-Transaction-Id": "txn-abc",
    "X-Opal-Request-Id": "req-abc",
    "X-Opal-Causation-Id": "cause-abc",
}


def test_valid_supplied_ids_are_preserved_and_echoed(client):
    resp = client.get("/healthz", headers=IDS)
    assert resp.status_code == 200
    for name, value in IDS.items():
        assert resp.headers[name] == value


def test_missing_transaction_and_request_ids_are_generated(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers["X-Opal-Transaction-Id"]
    assert resp.headers["X-Opal-Request-Id"]
    # Session and causation IDs are context-only: never invented by the service.
    assert "x-opal-session-id" not in resp.headers
    assert "x-opal-causation-id" not in resp.headers


def test_partial_supplied_ids_keep_client_values(client):
    resp = client.get("/healthz", headers={"X-Opal-Session-Id": "sess-1", "X-Opal-Causation-Id": "cause-1"})
    assert resp.headers["X-Opal-Session-Id"] == "sess-1"
    assert resp.headers["X-Opal-Causation-Id"] == "cause-1"
    assert resp.headers["X-Opal-Transaction-Id"]
    assert resp.headers["X-Opal-Request-Id"]


def test_generated_request_ids_are_unique_per_request(client):
    a, b = client.get("/healthz"), client.get("/healthz")
    assert a.headers["X-Opal-Request-Id"] != b.headers["X-Opal-Request-Id"]


@pytest.mark.parametrize("bad", ["has space", "", "has\ttab", "has\x7fdel", "x" * 129])
def test_invalid_identifiers_are_rejected_with_422(client, bad):
    client.skip_contract = True  # the v1.0 contract does not document these 422s
    for name in CORRELATION_HEADERS:
        resp = client.get("/healthz", headers={name: bad})
        assert resp.status_code == 422, (name, bad, resp.text)
        assert resp.json()["detail"], resp.text
        # Correlation headers are stamped even on the rejection.
        assert resp.headers["X-Opal-Transaction-Id"]


def test_every_invalid_identifier_is_reported(client):
    client.skip_contract = True
    resp = client.get("/healthz", headers={"X-Opal-Session-Id": "bad id", "X-Opal-Request-Id": "also bad"})
    assert resp.status_code == 422
    locs = {tuple(e["loc"]) for e in resp.json()["detail"]}
    assert locs == {("header", "X-Opal-Session-Id"), ("header", "X-Opal-Request-Id")}


def test_ids_echoed_on_error_responses(client):
    resp = client.get("/api/v1/engines/nope", headers=IDS)
    assert resp.status_code == 404
    for name, value in IDS.items():
        assert resp.headers[name] == value


def test_ids_echoed_on_binary_and_streamed_responses(client):
    make_voice(client, "v1")
    resp = client.post("/api/v1/voices/v1/test", headers=IDS)
    assert resp.status_code == 200 and resp.headers["content-type"] == "audio/wav"
    with client.stream("POST", "/api/v1/generate/stream", json={"text": "Hello.", "voice_id": "v1"}, headers=IDS) as s:
        assert s.status_code == 200
        for name, value in IDS.items():
            assert s.headers[name] == value
    for name, value in IDS.items():
        assert resp.headers[name] == value


def test_identifiers_are_available_on_request_state():
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)

    @app.get("/whoami")
    def whoami(request: Request):
        ids = correlation_ids(request)
        return {"session": ids.session_id, "transaction": ids.transaction_id, "request": ids.request_id}

    with TestClient(app) as c:
        body = c.get("/whoami", headers=IDS).json()
    assert body == {"session": "sess-abc", "transaction": "txn-abc", "request": "req-abc"}


def test_concurrent_requests_do_not_leak_identifiers(live_server):
    def call(i: int) -> None:
        headers = {**IDS, "X-Opal-Session-Id": f"sess-{i}", "X-Opal-Transaction-Id": f"txn-{i}"}
        resp = httpx.get(f"{live_server}/api/v1/health/", headers=headers, timeout=30)
        assert resp.status_code == 200
        assert resp.headers["X-Opal-Session-Id"] == f"sess-{i}"
        assert resp.headers["X-Opal-Transaction-Id"] == f"txn-{i}"
        assert resp.headers["X-Opal-Request-Id"] == "req-abc"
        assert resp.headers["X-Opal-Causation-Id"] == "cause-abc"

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(call, range(16)))
