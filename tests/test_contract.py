"""Contract tests: every operation is implemented, published and returns contract-conformant responses."""

from __future__ import annotations

import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
from contract_check import SPEC, check_response
from fastapi.routing import APIRoute

from conftest import ContractClient, make_profile, make_voice
from opal_talk.api.routes import router
from opal_talk.contract import contract_operations, load_contract

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = "contract/opal_talk.yaml"
TRACKED = "src/opal_talk/contract/opal_talk.yaml"


def test_packaged_contract_matches_repo_root():
    """`opal_talk.yaml` (repo root) and the packaged copy must stay identical.

    Two real files are kept (the root copy is the human-facing document; the packaged
    copy is what the service and tests load) because symlinks break on Windows
    checkouts and in built wheels. This test is the drift guard.
    """
    assert (ROOT / "opal_talk.yaml").read_bytes() == (
        files("opal_talk").joinpath(PACKAGED).read_bytes()
    )


def test_packaged_contract_is_a_readable_regular_file():
    """The packaged contract must exist as a regular file, not a missing or dangling entry."""
    resource = files("opal_talk").joinpath(PACKAGED)
    assert resource.is_file(), f"packaged contract {PACKAGED} is missing or not a regular file"
    assert resource.read_text(encoding="utf-8").startswith("openapi:")


def test_load_contract_returns_valid_v1_document():
    """load_contract() must yield a parseable OpenAPI document pinned to the implemented v1.0 API."""
    spec = load_contract()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["version"] == "1.0.0", "v1.2 contract must not be packaged silently"
    assert spec["paths"] and spec["components"]["schemas"]


def test_git_records_contract_as_regular_file():
    """Regression guard: Git must store the packaged contract as mode 100644.

    It was once committed as a symlink (mode 120000) whose 'target' was the YAML
    text itself, which makes `git checkout` fail on Linux CI.
    """
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("git metadata not available")
    out = subprocess.run(
        [git, "ls-files", "-s", "--", TRACKED],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out and out[0] == "100644", f"{TRACKED} has git mode {out[0] if out else '<untracked>'}, expected 100644"


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
