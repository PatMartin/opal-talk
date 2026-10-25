"""REST tests for engines, voices, profiles, settings and health, including validation/error paths."""

from __future__ import annotations

from conftest import make_profile, make_voice


class TestEngines:
    def test_list_and_get(self, client):
        ids = [e["engine_id"] for e in client.get("/api/v1/engines/").json()]
        assert ids == ["alt", "fake"]
        engine = client.get("/api/v1/engines/fake").json()
        assert engine["enabled"] and engine["available"]
        assert engine["options"] == {"available": True}
        assert engine["metadata"] == {"version": "0.0-test"}

    def test_update_enable_and_merge_options(self, client):
        resp = client.patch("/api/v1/engines/fake", json={"enabled": False})
        assert resp.json()["enabled"] is False
        resp = client.patch("/api/v1/engines/fake", json={"options": {"available": False}})
        assert resp.json()["options"] == {"available": False}
        assert resp.json()["available"] is False
        resp = client.patch("/api/v1/engines/fake", json={"options": {"available": None}})
        assert resp.json()["options"] == {}
        assert resp.json()["available"] is True

    def test_update_persists_across_restart(self, client, config, engines):
        client.patch("/api/v1/engines/fake", json={"enabled": False, "options": {"available": False}})
        from fastapi.testclient import TestClient

        from conftest import FakeEngine
        from opal_talk.api.app import create_app

        app2 = create_app(config, engines=[FakeEngine("fake"), FakeEngine("alt")])
        with TestClient(app2) as c2:
            engine = c2.get("/api/v1/engines/fake").json()
        app2.state.services.close()
        assert engine["enabled"] is False and engine["available"] is False

    def test_update_rejects_invalid_options(self, client):
        resp = client.patch("/api/v1/engines/fake", json={"options": {"nope": 1, "available": "yes"}})
        assert resp.status_code == 422
        locs = [d["loc"] for d in resp.json()["error"]["details"]["errors"]]
        assert ["body", "options", "nope"] in locs and ["body", "options", "available"] in locs

    def test_update_validation(self, client):
        assert client.patch("/api/v1/engines/fake", json={}).status_code == 422
        assert client.patch("/api/v1/engines/fake", json={"bogus": True}).status_code == 422
        assert client.patch("/api/v1/engines/fake", json={"enabled": None}).status_code == 422
        assert client.patch("/api/v1/engines/missing", json={"enabled": True}).status_code == 404

    def test_validate(self, client):
        assert client.post("/api/v1/engines/fake/validate").json() == {
            "valid": True, "errors": [], "warnings": [], "details": {"version": "0.0-test"}
        }
        client.patch("/api/v1/engines/fake", json={"enabled": False, "options": {"available": False}})
        result = client.post("/api/v1/engines/fake/validate").json()
        assert result["valid"] is False
        assert [e["code"] for e in result["errors"]] == ["engine_unavailable"]
        assert [w["code"] for w in result["warnings"]] == ["engine_disabled"]
        assert client.post("/api/v1/engines/missing/validate").status_code == 404


class TestVoices:
    def test_register_read_and_filter(self, client):
        voice = make_voice(client, "v1", description="d", engine_options={"pitch": 300})
        assert voice["native_sample_rate"] == 22050
        assert voice["language"] == "en-US"
        assert voice["metadata"] == {"kind": "tone"}
        assert voice["engine_options"] == {"pitch": 300}
        make_voice(client, "v2", engine_id="alt", enabled=False)
        assert [v["voice_id"] for v in client.get("/api/v1/voices/").json()] == ["v1", "v2"]
        assert [v["voice_id"] for v in client.get("/api/v1/voices/?engine_id=alt").json()] == ["v2"]
        assert [v["voice_id"] for v in client.get("/api/v1/voices/?enabled=true").json()] == ["v1"]
        assert client.get("/api/v1/voices/?enabled=maybe").status_code == 422
        assert client.get("/api/v1/voices/v1").json() == voice

    def test_register_errors(self, client):
        make_voice(client, "v1")
        base = {"voice_id": "v1", "name": "x", "engine_id": "fake", "engine_voice_ref": "tone-a"}
        assert client.post("/api/v1/voices/", json=base).status_code == 409
        assert client.post("/api/v1/voices/", json={**base, "voice_id": "v9", "engine_id": "nope"}).status_code == 404
        assert client.post("/api/v1/voices/", json={**base, "voice_id": "-bad"}).status_code == 422
        assert client.post("/api/v1/voices/", json={**base, "voice_id": "v9", "extra": 1}).status_code == 422
        resp = client.post("/api/v1/voices/", json={**base, "voice_id": "v9", "engine_voice_ref": "missing"})
        assert resp.status_code == 422
        assert resp.json()["error"]["details"]["errors"][0]["loc"] == ["body", "engine_voice_ref"]
        resp = client.post("/api/v1/voices/", json={**base, "voice_id": "v9", "engine_options": {"x": 1}})
        assert resp.status_code == 422
        assert resp.json()["error"]["details"]["errors"][0]["loc"] == ["body", "engine_options", "x"]

    def test_register_when_engine_unavailable_defers_resolution(self, client):
        client.patch("/api/v1/engines/fake", json={"options": {"available": False}})
        voice = make_voice(client, "v1", ref="anything")
        assert voice["native_sample_rate"] is None
        result = client.post("/api/v1/voices/v1/validate").json()
        assert result["valid"] is False and result["errors"][0]["code"] == "engine_unavailable"

    def test_update(self, client):
        make_voice(client, "v1")
        resp = client.patch("/api/v1/voices/v1", json={"engine_voice_ref": "tone-b", "description": "x"})
        assert resp.json()["native_sample_rate"] == 16000 and resp.json()["description"] == "x"
        assert client.patch("/api/v1/voices/v1", json={"description": None}).json()["description"] is None
        assert client.patch("/api/v1/voices/v1", json={"engine_id": "alt"}).status_code == 422  # alt lacks tone-b
        resp = client.patch("/api/v1/voices/v1", json={"engine_id": "alt", "engine_voice_ref": "tone-a"})
        assert resp.json()["engine_id"] == "alt" and resp.json()["native_sample_rate"] == 24000
        assert client.patch("/api/v1/voices/v1", json={}).status_code == 422
        assert client.patch("/api/v1/voices/v1", json={"name": None}).status_code == 422
        assert client.patch("/api/v1/voices/v1", json={"engine_voice_ref": "zzz"}).status_code == 422
        assert client.patch("/api/v1/voices/v1", json={"engine_id": "nope"}).status_code == 404
        assert client.patch("/api/v1/voices/zz", json={"name": "a"}).status_code == 404

    def test_update_conflicts(self, client):
        make_voice(client, "v1")
        make_profile(client, "p", "v1", is_active=True, engine_options={"pitch": 100})
        resp = client.patch("/api/v1/voices/v1", json={"enabled": False})
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "voice_in_use"

    def test_delete(self, client):
        make_voice(client, "v1")
        p = make_profile(client, "p", "v1")
        resp = client.delete("/api/v1/voices/v1")
        assert resp.status_code == 409 and resp.json()["error"]["details"]["profile_ids"] == [p["id"]]
        client.delete(f"/api/v1/profiles/{p['id']}")
        assert client.delete("/api/v1/voices/v1").status_code == 204
        assert client.get("/api/v1/voices/v1").status_code == 404
        assert client.delete("/api/v1/voices/v1").status_code == 404

    def test_validate(self, client):
        make_voice(client, "v1", enabled=False)
        result = client.post("/api/v1/voices/v1/validate").json()
        assert result["valid"] is True
        assert [w["code"] for w in result["warnings"]] == ["voice_disabled"]
        assert result["details"]["native_sample_rate"] == 22050
        assert client.post("/api/v1/voices/zz/validate").status_code == 404


class TestProfiles:
    def test_crud_and_single_active(self, client):
        make_voice(client, "v1")
        p1 = make_profile(client, "p1", "v1", is_active=True)
        assert p1 | {"created_at": None, "updated_at": None} == {
            "id": p1["id"], "name": "p1", "description": None, "is_active": True, "voice_id": "v1", "speed": 1.0,
            "volume": 1.0, "output_format": "wav", "sample_rate": None, "engine_options": {},
            "created_at": None, "updated_at": None,
        }
        p2 = make_profile(client, "p2", "v1", is_active=True, speed=2.0, sample_rate=8000)
        assert client.get(f"/api/v1/profiles/{p1['id']}").json()["is_active"] is False
        resp = client.patch(f"/api/v1/profiles/{p1['id']}", json={"is_active": True, "sample_rate": None})
        assert resp.json()["is_active"] is True
        assert client.get(f"/api/v1/profiles/{p2['id']}").json()["is_active"] is False
        actives = [p for p in client.get("/api/v1/profiles/").json() if p["is_active"]]
        assert [p["id"] for p in actives] == [p1["id"]]
        assert client.post(f"/api/v1/profiles/{p2['id']}/activate").json()["is_active"] is True
        assert client.get("/api/v1/health/").json()["active_profile_id"] == p2["id"]

    def test_errors(self, client):
        make_voice(client, "v1")
        make_voice(client, "v2", enabled=False)
        p1 = make_profile(client, "p1", "v1", is_active=True)
        assert client.post("/api/v1/profiles/", json={"name": "x", "voice_id": "nope"}).status_code == 404
        assert client.post("/api/v1/profiles/", json={"name": "p1", "voice_id": "v1"}).status_code == 409
        assert client.post("/api/v1/profiles/", json={"name": "x", "voice_id": "v1", "speed": 9}).status_code == 422
        assert client.post("/api/v1/profiles/", json={"name": "x", "voice_id": "v1",
                                                       "output_format": "mp3"}).status_code == 422
        resp = client.post("/api/v1/profiles/", json={"name": "x", "voice_id": "v1", "engine_options": {"q": 1}})
        assert resp.status_code == 422
        resp = client.post("/api/v1/profiles/", json={"name": "x", "voice_id": "v2", "is_active": True})
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "voice_disabled"
        p2 = make_profile(client, "p2", "v2")
        assert client.post(f"/api/v1/profiles/{p2['id']}/activate").status_code == 409
        assert client.delete(f"/api/v1/profiles/{p1['id']}").json()["error"]["code"] == "profile_active"
        assert client.patch(f"/api/v1/profiles/{p2['id']}", json={"name": "p1"}).status_code == 409
        assert client.patch(f"/api/v1/profiles/{p2['id']}", json={"voice_id": "zz"}).status_code == 404
        assert client.patch(f"/api/v1/profiles/{p2['id']}", json={}).status_code == 422
        assert client.get("/api/v1/profiles/0").status_code == 422
        assert client.get("/api/v1/profiles/abc").status_code == 422
        assert client.get("/api/v1/profiles/999").status_code == 404
        assert client.delete("/api/v1/profiles/999").status_code == 404
        assert client.post("/api/v1/profiles/999/activate").status_code == 404

    def test_activation_requires_usable_engine(self, client):
        make_voice(client, "v1")
        p = make_profile(client, "p1", "v1")
        client.patch("/api/v1/engines/fake", json={"enabled": False})
        resp = client.post(f"/api/v1/profiles/{p['id']}/activate")
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "engine_disabled"
        client.patch("/api/v1/engines/fake", json={"enabled": True, "options": {"available": False}})
        assert client.post(f"/api/v1/profiles/{p['id']}/activate").json()["error"]["code"] == "engine_unavailable"


class TestSettings:
    def test_crud(self, client):
        assert client.get("/api/v1/settings/").json() == {}
        assert client.get("/api/v1/settings/foo").status_code == 404
        setting = client.put("/api/v1/settings/foo", json={"value": "bar"}).json()
        assert setting["key"] == "foo" and setting["value"] == "bar"
        updated = client.put("/api/v1/settings/foo", json={"value": "baz"}).json()
        assert updated["id"] == setting["id"] and updated["value"] == "baz"
        assert client.get("/api/v1/settings/").json() == {"foo": "baz"}
        assert client.delete("/api/v1/settings/foo").status_code == 204
        assert client.delete("/api/v1/settings/foo").status_code == 404

    def test_known_settings_are_validated(self, client):
        assert client.put("/api/v1/settings/max_text_chars", json={"value": "abc"}).status_code == 422
        assert client.put("/api/v1/settings/default_output_format", json={"value": "mp3"}).status_code == 422
        assert client.put("/api/v1/settings/max_text_chars", json={"value": "10"}).status_code == 200
        assert client.put("/api/v1/settings/x", json={"value": 5}).status_code == 422
        assert client.put("/api/v1/settings/x", json={}).status_code == 422


class TestHealth:
    def test_states(self, client):
        assert client.get("/api/v1/health/").json()["status"] == "ok"
        client.patch("/api/v1/engines/alt", json={"options": {"available": False}})
        assert client.get("/api/v1/health/").json()["status"] == "degraded"
        client.patch("/api/v1/engines/fake", json={"enabled": False})
        health = client.get("/api/v1/health/").json()
        assert health["status"] == "error"
        assert {e["engine_id"]: e["available"] for e in health["engines"]} == {"alt": False, "fake": True}

    def test_unknown_route_uses_error_shape(self, client):
        resp = client.get("/api/v1/nothing")
        assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"
