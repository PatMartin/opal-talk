"""Backup, restore, import, export and configuration validation."""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from conftest import make_profile, make_voice


@pytest.fixture
def populated(client):
    make_voice(client, "v1", engine_options={"pitch": 200})
    make_voice(client, "v2", ref="tone-b", description="second")
    make_profile(client, "main", "v1", is_active=True, speed=1.5, engine_options={"pitch": 220})
    client.put("/api/v1/settings/chunk_max_chars", json={"value": "120"})
    client.patch("/api/v1/engines/alt", json={"enabled": False})


def write_config(config, files: dict[str, str]) -> None:
    config.config_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (config.config_dir / name).write_text(text)


def test_export_contains_full_configuration(client, populated):
    data = client.get("/api/v1/backup/export").json()
    assert [v["voice_id"] for v in data["voices"]] == ["v1", "v2"]
    assert data["profiles"][0]["name"] == "main"
    assert data["settings"] == {"chunk_max_chars": "120"}
    assert {e["engine_id"]: e["enabled"] for e in data["engines"]} == {"alt": False, "fake": True}


def test_backup_roundtrip_restores_exact_state(client, populated):
    before = client.get("/api/v1/backup/export").json()
    filename = client.post("/api/v1/backup/create").json()["filename"]
    assert filename.startswith("opal-talk-") and filename.endswith(".tar.gz")

    client.post("/api/v1/profiles/", json={"name": "extra", "voice_id": "v2"})
    client.delete("/api/v1/settings/chunk_max_chars")
    client.patch("/api/v1/engines/alt", json={"enabled": True})
    client.patch("/api/v1/voices/v2", json={"name": "renamed"})

    result = client.post(f"/api/v1/backup/restore/{filename}").json()
    assert result["success"] is True
    after = client.get("/api/v1/backup/export").json()
    for key in ("engines", "voices", "profiles", "settings"):
        assert after[key] == before[key], key


def test_backup_archive_contents(client, populated):
    filename = client.post("/api/v1/backup/create").json()["filename"]
    resp = client.get(f"/api/v1/backup/download/{filename}")
    assert resp.headers["content-type"] == "application/octet-stream"
    assert filename in resp.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        assert tar.getnames() == ["opal-talk-config.json"]
        payload = json.loads(tar.extractfile("opal-talk-config.json").read())
    assert payload["voices"][0]["voice_id"] == "v1"
    listing = client.get("/api/v1/backup/list").json()
    assert listing[0]["filename"] == filename and listing[0]["size_bytes"] == len(resp.content)


def test_multiple_backups_get_unique_names(client):
    names = {client.post("/api/v1/backup/create").json()["filename"] for _ in range(3)}
    assert len(names) == 3


@pytest.mark.parametrize("name", ["..", "a..b.tar.gz", "..%5Cetc", "x\\y"])
def test_traversal_is_rejected(client, name):
    for method, url in (("GET", f"/api/v1/backup/download/{name}"), ("POST", f"/api/v1/backup/restore/{name}"),
                        ("DELETE", f"/api/v1/backup/{name}")):
        resp = client.request(method, url)
        assert resp.status_code in (404, 422), (method, url, resp.status_code)
        assert resp.status_code != 200


def test_unknown_backup_is_404(client):
    assert client.get("/api/v1/backup/download/opal-talk-2020-01-01T000000Z.tar.gz").status_code == 404
    assert client.post("/api/v1/backup/restore/nothing.tar.gz").status_code == 404
    assert client.delete("/api/v1/backup/nothing.tar.gz").status_code == 404


def test_corrupt_backup_restore_is_conflict_and_changes_nothing(client, populated, config):
    before = client.get("/api/v1/backup/export").json()
    bad = config.backup_dir / "opal-talk-2020-01-01T000000Z.tar.gz"
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a tarball")
    resp = client.post(f"/api/v1/backup/restore/{bad.name}")
    assert resp.status_code == 409 and resp.json()["code"] == "invalid_backup"
    assert client.get("/api/v1/backup/export").json()["voices"] == before["voices"]


def test_invalid_backup_content_is_rejected_atomically(client, populated, config):
    export = client.get("/api/v1/backup/export").json()
    export["profiles"][0]["voice_id"] = "ghost"
    payload = json.dumps(export).encode()
    path = config.backup_dir / "opal-talk-2020-01-02T000000Z.tar.gz"
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("opal-talk-config.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    client.post("/api/v1/profiles/", json={"name": "keep-me", "voice_id": "v2"})
    resp = client.post(f"/api/v1/backup/restore/{path.name}")
    assert resp.status_code == 409 and resp.json()["code"] == "backup_invalid"
    assert "keep-me" in [p["name"] for p in client.get("/api/v1/profiles/").json()]


def test_delete_backup(client):
    filename = client.post("/api/v1/backup/create").json()["filename"]
    assert client.delete(f"/api/v1/backup/{filename}").status_code == 204
    assert client.get("/api/v1/backup/list").json() == []


def test_import_from_config_dir(client, config):
    write_config(config, {
        "engines.yaml": "version: 1\nengines:\n  alt:\n    enabled: false\n  ghost:\n    enabled: true\n",
        "voices.yaml": "version: 1\nvoices:\n  - {voice_id: a, name: A, engine_id: fake, engine_voice_ref: tone-a}\n"
                       "  - {voice_id: b, name: B, engine_id: fake, engine_voice_ref: tone-b, enabled: false}\n",
        "profiles.yaml": "version: 1\nprofiles:\n  - {name: one, voice_id: a, is_active: true, speed: 1.25}\n"
                         "  - {name: two, voice_id: b, output_format: pcm}\n",
        "settings.yaml": "version: 1\nsettings:\n  chunk_max_chars: 250\n  flag: true\n",
    })
    result = client.post("/api/v1/backup/import").json()
    assert result["success"] is True, result
    assert [w["code"] for w in result["validation"]["warnings"]] == ["engine_not_installed"]
    assert [v["voice_id"] for v in client.get("/api/v1/voices/").json()] == ["a", "b"]
    assert client.get("/api/v1/voices/a").json()["native_sample_rate"] == 22050
    profiles = client.get("/api/v1/profiles/").json()
    assert [(p["id"], p["name"], p["is_active"]) for p in profiles] == [(1, "one", True), (2, "two", False)]
    assert client.get("/api/v1/settings/").json() == {"chunk_max_chars": "250", "flag": "true"}
    assert client.get("/api/v1/engines/alt").json()["enabled"] is False
    assert client.post("/api/v1/generate", json={"text": "hi"}).headers["x-opal-voice-id"] == "a"


def test_import_only_replaces_present_sections(client, config, populated):
    write_config(config, {"settings.yaml": "version: 1\nsettings:\n  default_volume: 0.5\n"})
    assert client.post("/api/v1/backup/import").json()["success"] is True
    assert [v["voice_id"] for v in client.get("/api/v1/voices/").json()] == ["v1", "v2"]
    assert client.get("/api/v1/settings/").json() == {"default_volume": "0.5"}


@pytest.mark.parametrize(
    "files,code",
    [
        ({"profiles.yaml": "profiles:\n  - {name: x, voice_id: ghost}\n"}, "voice_not_found"),
        ({"profiles.yaml": "profiles:\n  - {name: x, voice_id: v1, is_active: true}\n"
                           "  - {name: y, voice_id: v1, is_active: true}\n"}, "multiple_active_profiles"),
        ({"profiles.yaml": "profiles:\n  - {name: x, voice_id: v1}\n  - {name: x, voice_id: v1}\n"}, "duplicate_profile"),
        ({"voices.yaml": "voices:\n  - {voice_id: q, name: Q, engine_id: nope, engine_voice_ref: r}\n"},
         "engine_not_found"),
        ({"voices.yaml": "voices:\n  - {voice_id: q, name: Q, engine_id: fake}\n"}, "schema_invalid"),
        ({"settings.yaml": "settings:\n  max_text_chars: lots\n"}, "invalid_setting"),
        ({"engines.yaml": "engines:\n  fake:\n    options: {bogus: 1}\n"}, "unsupported_option"),
        ({"voices.yaml": "voices: [\n"}, "yaml_invalid"),
    ],
)
def test_invalid_import_applies_nothing(client, config, populated, files, code):
    before = client.get("/api/v1/backup/export").json()
    write_config(config, files)
    result = client.post("/api/v1/backup/import").json()
    assert result["success"] is False
    assert code in [e["code"] for e in result["validation"]["errors"]]
    after = client.get("/api/v1/backup/export").json()
    for key in ("engines", "voices", "profiles", "settings"):
        assert after[key] == before[key]


def test_import_without_config_files_is_conflict(client):
    resp = client.post("/api/v1/backup/import")
    assert resp.status_code == 409 and resp.json()["code"] == "config_not_found"


def test_import_unresolvable_resource_is_warning(client, config):
    write_config(config, {"voices.yaml": "voices:\n  - {voice_id: q, name: Q, engine_id: fake, engine_voice_ref: gone}\n"})
    result = client.post("/api/v1/backup/import").json()
    assert result["success"] is True
    assert [w["code"] for w in result["validation"]["warnings"]] == ["model_not_found"]
    live = client.get("/api/v1/backup/validate").json()
    assert live["valid"] is False and live["errors"][0]["field"] == "voices.q.engine_voice_ref"


def test_validate_current_config(client, populated):
    result = client.get("/api/v1/backup/validate").json()
    assert result["valid"] is True
    assert result["details"] == {"engines": 2, "voices": 2, "profiles": 1, "settings": 1}


def test_first_start_seeds_from_config_dir(config, engines):
    from fastapi.testclient import TestClient

    from opal_talk.api.app import create_app

    write_config(config, {"voices.yaml": "voices:\n  - {voice_id: seeded, name: S, engine_id: fake, "
                                         "engine_voice_ref: tone-a}\n"})
    app = create_app(config, engines=engines)
    with TestClient(app) as c:
        assert [v["voice_id"] for v in c.get("/api/v1/voices/").json()] == ["seeded"]
    app.state.services.close()
