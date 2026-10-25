"""CLI tests: commands run against the real REST app (in-process) through the HTTP client."""

from __future__ import annotations

import json
import shlex
import wave

import pytest
from contract_check import check_response, match_operation
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from opal_talk.cli import main as cli
from opal_talk.cli.client import ApiClient
from opal_talk.contract import contract_operations


class RecordingTestClient(TestClient):
    def __init__(self, app):
        super().__init__(app)
        self.ops: list[str] = []

    def send(self, request, *args, **kwargs):  # type: ignore[override]
        resp = super().send(request, *args, **kwargs)
        op = match_operation(request.method, request.url.path)
        if op:
            self.ops.append(op["operationId"])
            if not kwargs.get("stream"):
                check_response(resp)
        return resp


@pytest.fixture
def http(app, monkeypatch):
    with RecordingTestClient(app) as tc:
        monkeypatch.setattr(cli, "client_factory", lambda url, timeout: ApiClient(url, timeout, http=tc))
        yield tc


@pytest.fixture
def run(http):
    runner = CliRunner()

    def invoke(args: str, code: int = 0, stdin: str | None = None):
        result = runner.invoke(cli.app, ["talk", *shlex.split(args)], input=stdin)
        assert result.exit_code == code, f"{args}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        return result

    return invoke


def test_full_cli_workflow(run, http, tmp_path, config):
    out = run("--json engines list").stdout
    assert [e["engine_id"] for e in json.loads(out)] == ["alt", "fake"]
    assert "fake" in run("engines list").stdout
    assert json.loads(run("--json engines get fake").stdout)["engine_id"] == "fake"
    assert json.loads(run("engines update alt --disable -O available=true").stdout)["enabled"] is False
    assert "valid" in run("engines validate fake").stdout

    run('voices add v1 --name "Voice One" --engine fake --ref tone-a -O pitch=300 --language en-US')
    run("voices add v2 --name Two --engine fake --ref tone-b --disabled")
    listing = json.loads(run("--json voices list --engine fake --enabled").stdout)
    assert [v["voice_id"] for v in listing] == ["v1"]
    assert json.loads(run("voices get v1").stdout)["engine_options"] == {"pitch": 300}
    assert json.loads(run("voices update v2 --enabled --description hi").stdout)["description"] == "hi"
    assert json.loads(run("voices update v2 --clear-description").stdout)["description"] is None
    run("voices validate v1")
    wav = tmp_path / "test.wav"
    result = run(f"voices test v1 -o {wav.as_posix()} --text Hello --sample-rate 16000")
    with wave.open(str(wav)) as w:
        assert w.getframerate() == 16000
    assert "voice=v1" in result.stderr

    p = json.loads(run("profiles add main --voice v1 --active --speed 1.5 --format pcm").stdout)
    assert p["is_active"] and p["output_format"] == "pcm"
    p2 = json.loads(run("profiles add other --voice v2 --options-json '{\"pitch\": 600}'").stdout)
    assert "main" in run("profiles list").stdout
    assert json.loads(run(f"profiles get {p['id']}").stdout)["speed"] == 1.5
    assert json.loads(run(f"profiles update {p2['id']} --volume 0.5 --native-rate").stdout)["volume"] == 0.5
    assert json.loads(run(f"profiles activate {p2['id']}").stdout)["is_active"] is True
    run(f"profiles remove {p['id']}")

    pcm = tmp_path / "out.pcm"
    result = run(f'generate "Hello there. General Kenobi." -o {pcm.as_posix()} --format pcm --sample-rate 8000')
    assert pcm.stat().st_size > 0 and "8000 Hz" in result.stderr
    streamed = tmp_path / "stream.pcm"
    run(f"generate - --stream -o {streamed.as_posix()} --voice v1", stdin="Streaming via stdin.")
    assert streamed.stat().st_size > 0
    stdout_audio = run("generate Hi --voice v1 -o -").stdout_bytes
    assert stdout_audio[:4] == b"RIFF"

    run("settings set chunk_max_chars 200")
    assert json.loads(run("settings list").stdout) == {"chunk_max_chars": "200"}
    assert json.loads(run("settings get chunk_max_chars").stdout)["value"] == "200"
    run("settings delete chunk_max_chars")

    assert "status:" in run("health").stdout
    assert json.loads(run("info").stdout)["name"] == "opal-talk-svc"
    assert run("ping").stdout.strip() == "ok"

    filename = json.loads(run("backup create").stdout)["filename"]
    assert filename in run("backup list").stdout
    target = tmp_path / "b.tar.gz"
    run(f"backup download {filename} -o {target.as_posix()}")
    assert target.stat().st_size > 0
    assert json.loads(run(f"backup restore {filename}").stdout)["success"] is True
    export = tmp_path / "export.json"
    run(f"backup export -o {export.as_posix()}")
    assert json.loads(export.read_text())["voices"][0]["voice_id"] == "v1"
    run("backup validate")
    config.config_dir.mkdir(parents=True, exist_ok=True)
    (config.config_dir / "settings.yaml").write_text("settings:\n  default_volume: 0.9\n")
    assert json.loads(run("backup import").stdout)["success"] is True
    run(f"backup delete {filename}")
    run("voices remove v1")
    result = run("voices remove v2", code=1)  # still referenced by profile 'other'
    assert "voice_in_use" in result.stderr


def test_every_operation_is_reachable_from_cli(run, http, tmp_path, config):
    """Parity: each contract operation is invoked by its mapped CLI command."""
    test_full_cli_workflow(run, http, tmp_path, config)
    missing = set(contract_operations()) - set(http.ops)
    assert not missing, f"CLI never called: {sorted(missing)}"


def test_error_reporting(run):
    result = run("voices get ghost", code=1)
    assert "HTTP 404 voice_not_found" in result.stderr
    result = run("voices add bad --name x --engine fake --ref nope", code=1)
    assert "validation error" in result.stderr and "engine_voice_ref" in result.stderr
    result = run('generate "x" -o /dev/null', code=1)
    assert "no_voice" in result.stderr
    result = run('generate "x" --stream -o /dev/null', code=1)
    assert "409 no_voice" in result.stderr
    run("engines update fake", code=2)
    run("generate x --stream --format wav -o -", code=2)
    run("voices add a --name a --engine fake --ref tone-a -O novalue", code=2)


def test_invalid_validation_result_exits_nonzero(run):
    run("voices add v --name V --engine fake --ref tone-a")
    run("engines update fake -O available=false")
    result = run("voices validate v", code=1)
    assert "INVALID" in result.stdout and "engine_unavailable" in result.stdout


def test_unreachable_server(monkeypatch):
    monkeypatch.setattr(cli, "client_factory", lambda url, timeout: ApiClient("http://127.0.0.1:9", 1))
    result = CliRunner().invoke(cli.app, ["talk", "ping"])
    assert result.exit_code == 2 and "cannot reach" in result.stderr
