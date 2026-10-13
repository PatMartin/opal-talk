"""Contract tests: every operation is implemented, published and returns contract-conformant responses."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from conftest import ContractClient, make_profile, make_voice
from contract import SPEC, check_response
from opal_talk.api.routes import router
from opal_talk.contract import contract_operations


def test_every_contract_operation_has_a_server_route():
    routes = {
        r.operation_id: (method, r.path)
        for r in router.routes
        if isinstance(r, APIRoute)
        for method in r.methods
    }
    for op_id, (method, path) in contract_operations().items():
        assert op_id in routes, f"operation {op_id} is not implemented"
        assert routes[op_id] == (method, path), f"{op_id} bound to {routes[op_id]}, contract says {(method, path)}"


def test_no_undocumented_api_routes():
    documented = set(contract_operations().values())
    for r in router.routes:
        if isinstance(r, APIRoute):
            for method in r.methods:
                assert (method, r.path) in documented, f"route {method} {r.path} is not in the contract"


def test_published_openapi_is_the_contract(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json() == SPEC


class RecordingClient(ContractClient):
    seen: set[str]

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        resp = super(ContractClient, self).request(method, url, *args, **kwargs)
        op = check_response(resp)
        if op:
            self.seen.add(op["operationId"])
        return resp


@pytest.fixture
def recording(app):
    with RecordingClient(app) as c:
        c.seen = set()
        yield c


def test_walkthrough_exercises_every_operation_with_conformant_responses(recording, config):
    c = recording
    assert c.get("/").json()["name"] == "opal-talk-svc"
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/api/v1/health/").status_code == 200

    assert c.get("/api/v1/engines/").status_code == 200
    assert c.get("/api/v1/engines/fake").status_code == 200
    assert c.get("/api/v1/engines/nope").status_code == 404
    assert c.patch("/api/v1/engines/fake", json={"enabled": True}).status_code == 200
    assert c.patch("/api/v1/engines/fake", json={}).status_code == 422
    assert c.post("/api/v1/engines/fake/validate").json()["valid"] is True

    make_voice(c, "v1")
    make_voice(c, "v2", ref="tone-b")
    assert c.get("/api/v1/voices/", params={"engine_id": "fake", "enabled": True}).status_code == 200
    assert c.get("/api/v1/voices/v1").status_code == 200
    assert c.patch("/api/v1/voices/v1", json={"name": "Voice One"}).status_code == 200
    assert c.post("/api/v1/voices/v1/validate").json()["valid"] is True
    assert c.post("/api/v1/voices/v1/test").status_code == 200
    assert c.post("/api/v1/voices/v1/test", json={"output_format": "pcm"}).status_code == 200

    p1 = make_profile(c, "p1", "v1", is_active=True)
    p2 = make_profile(c, "p2", "v2")
    assert c.get("/api/v1/profiles/").status_code == 200
    assert c.get(f"/api/v1/profiles/{p1['id']}").status_code == 200
    assert c.patch(f"/api/v1/profiles/{p2['id']}", json={"speed": 1.5}).status_code == 200
    assert c.post(f"/api/v1/profiles/{p2['id']}/activate").status_code == 200
    assert c.delete(f"/api/v1/profiles/{p1['id']}").status_code == 204

    assert c.post("/api/v1/generate", json={"text": "Hello there."}).status_code == 200
    assert c.post("/api/v1/generate/stream", json={"text": "Hello there."}).status_code == 200

    assert c.put("/api/v1/settings/greeting", json={"value": "hi"}).status_code == 200
    assert c.get("/api/v1/settings/").json() == {"greeting": "hi"}
    assert c.get("/api/v1/settings/greeting").status_code == 200
    assert c.delete("/api/v1/settings/greeting").status_code == 204

    filename = c.post("/api/v1/backup/create").json()["filename"]
    assert c.get("/api/v1/backup/list").status_code == 200
    assert c.get(f"/api/v1/backup/download/{filename}").status_code == 200
    assert c.post(f"/api/v1/backup/restore/{filename}").status_code == 200
    assert c.get("/api/v1/backup/export").status_code == 200
    assert c.get("/api/v1/backup/validate").status_code == 200
    config.config_dir.mkdir(parents=True, exist_ok=True)
    (config.config_dir / "settings.yaml").write_text("version: 1\nsettings:\n  chunk_max_chars: 300\n")
    assert c.post("/api/v1/backup/import").json()["success"] is True
    assert c.delete(f"/api/v1/backup/{filename}").status_code == 204

    assert c.delete("/api/v1/voices/v1").status_code == 204

    missing = set(contract_operations()) - c.seen
    assert not missing, f"operations not exercised: {sorted(missing)}"
