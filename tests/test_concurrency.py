"""ETag/If-Match optimistic concurrency on configuration mutations (contract 1.2.0)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from conftest import make_profile, make_voice

PRECONDITION_CALLS = [
    ("patch", "/api/v1/engines/fake", {"json": {"enabled": False}}),
    ("patch", "/api/v1/voices/v1", {"json": {"name": "X"}}),
    ("delete", "/api/v1/voices/v1", {}),
    ("patch", "/api/v1/profiles/1", {"json": {"speed": 2.0}}),
    ("delete", "/api/v1/profiles/1", {}),
    ("put", "/api/v1/settings/x", {"json": {"value": "1"}}),
    ("delete", "/api/v1/settings/x", {}),
]


def etag(client) -> str:
    return client.get("/api/v1/settings/").headers["etag"]


def test_config_gets_return_strong_etag(client):
    make_voice(client, "v1")
    p = make_profile(client, "p", "v1")
    client.put("/api/v1/settings/greeting", json={"value": "hi"})
    for path in (
        "/api/v1/engines/", "/api/v1/engines/fake",
        "/api/v1/voices/", "/api/v1/voices/v1",
        "/api/v1/profiles/", f"/api/v1/profiles/{p['id']}",
        "/api/v1/settings/", "/api/v1/settings/greeting",
    ):
        tag = client.get(path).headers.get("etag")
        assert tag and tag.startswith('"r'), (path, tag)


@pytest.mark.parametrize("method,url,kw", PRECONDITION_CALLS)
def test_missing_if_match_is_428(client, method, url, kw):
    make_voice(client, "v1")
    make_profile(client, "p", "v1")  # becomes profile id 1
    client.skip_contract = True
    client.skip_if_match = True
    resp = client.request(method, url, **kw)
    assert resp.status_code == 428, (method, url, resp.text)
    assert resp.json()["error"]["code"] == "precondition_required"


@pytest.mark.parametrize("method,url,kw", PRECONDITION_CALLS)
def test_stale_if_match_is_412(client, method, url, kw):
    make_voice(client, "v1")
    make_profile(client, "p", "v1")
    old = etag(client)
    client.put("/api/v1/settings/other", json={"value": "1"})  # bumps the revision
    client.skip_contract = True
    resp = client.request(method, url, headers={"If-Match": old}, **kw)
    assert resp.status_code == 412, (method, url, resp.text)
    assert resp.json()["error"]["code"] == "precondition_failed"


def test_malformed_if_match_is_412(client):
    client.skip_contract = True
    resp = client.patch("/api/v1/engines/fake", json={"enabled": True}, headers={"If-Match": "nonsense"})
    assert resp.status_code == 412


def test_fresh_if_match_succeeds_and_bumps_revision(client):
    old = etag(client)
    resp = client.put("/api/v1/settings/greeting", json={"value": "hi"}, headers={"If-Match": old})
    assert resp.status_code == 200
    assert etag(client) != old
    # The same precondition now fails — the revision moved on.
    client.skip_contract = True
    assert client.put("/api/v1/settings/greeting", json={"value": "hi2"}, headers={"If-Match": old}).status_code == 412


def test_simultaneous_updates_single_winner(client):
    """Two requests carrying the same base revision: exactly one may commit."""
    base = etag(client)
    client.skip_contract = True
    r1 = client.patch("/api/v1/engines/fake", json={"enabled": False}, headers={"If-Match": base})
    r2 = client.patch("/api/v1/engines/fake", json={"enabled": True}, headers={"If-Match": base})
    assert sorted([r1.status_code, r2.status_code]) == [200, 412]


def test_concurrent_writers_race_once(live_server):
    """N writers racing with the same ETag: exactly one commits (check+mutate is atomic)."""
    base = httpx.get(f"{live_server}/api/v1/settings/").headers["etag"]

    def attempt(i: int) -> int:
        return httpx.put(
            f"{live_server}/api/v1/settings/race-{i % 2}",
            json={"value": str(i)}, headers={"If-Match": base}, timeout=30,
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count(200) == 1, results
    assert results.count(412) == 7, results


def test_config_import_bumps_revision(client, config):
    before = etag(client)
    config.config_dir.mkdir(parents=True, exist_ok=True)
    (config.config_dir / "settings.yaml").write_text("version: 1\nsettings:\n  chunk_max_chars: 250\n")
    assert client.post("/api/v1/backup/import").json()["success"] is True
    assert etag(client) != before
    assert client.get("/api/v1/settings/chunk_max_chars").json()["value"] == "250"


def test_backup_restore_bumps_revision_and_restores_content(client):
    client.put("/api/v1/settings/greeting", json={"value": "x"})
    filename = client.post("/api/v1/backup/create").json()["filename"]
    client.delete("/api/v1/settings/greeting")
    before = etag(client)
    assert client.post(f"/api/v1/backup/restore/{filename}").status_code == 200
    assert etag(client) != before
    assert client.get("/api/v1/settings/greeting").json()["value"] == "x"
