"""`opal talk ...` command line. Every command calls the OPAL Talk REST API (see COMMANDS)."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import typer

from opal_talk.cli.client import ApiCallError, ApiClient, api_url_from_env

# REST operationId -> CLI command (relative to `opal talk`). Verified by the parity tests.
COMMANDS: dict[str, str] = {
    "list_engines_api_v1_engines__get": "engines list",
    "get_engine_api_v1_engines__engine_id__get": "engines get",
    "update_engine_api_v1_engines__engine_id__patch": "engines update",
    "validate_engine_api_v1_engines__engine_id__validate_post": "engines validate",
    "list_voices_api_v1_voices__get": "voices list",
    "create_voice_api_v1_voices__post": "voices add",
    "get_voice_api_v1_voices__voice_id__get": "voices get",
    "update_voice_api_v1_voices__voice_id__patch": "voices update",
    "delete_voice_api_v1_voices__voice_id__delete": "voices remove",
    "validate_voice_api_v1_voices__voice_id__validate_post": "voices validate",
    "test_voice_api_v1_voices__voice_id__test_post": "voices test",
    "list_profiles_api_v1_profiles__get": "profiles list",
    "create_profile_api_v1_profiles__post": "profiles add",
    "get_profile_api_v1_profiles__profile_id__get": "profiles get",
    "update_profile_api_v1_profiles__profile_id__patch": "profiles update",
    "delete_profile_api_v1_profiles__profile_id__delete": "profiles remove",
    "activate_profile_api_v1_profiles__profile_id__activate_post": "profiles activate",
    "generate_audio_api_v1_generate_post": "generate",
    "stream_audio_api_v1_generate_stream_post": "generate --stream",
    "get_all_settings_api_v1_settings__get": "settings list",
    "get_setting_api_v1_settings__key__get": "settings get",
    "upsert_setting_api_v1_settings__key__put": "settings set",
    "delete_setting_api_v1_settings__key__delete": "settings delete",
    "health_check_api_v1_health__get": "health",
    "list_backups_api_v1_backup_list_get": "backup list",
    "create_backup_api_v1_backup_create_post": "backup create",
    "download_backup_api_v1_backup_download__filename__get": "backup download",
    "restore_backup_api_v1_backup_restore__filename__post": "backup restore",
    "delete_backup_api_v1_backup__filename__delete": "backup delete",
    "export_config_api_v1_backup_export_get": "backup export",
    "import_config_api_v1_backup_import_post": "backup import",
    "validate_config_api_v1_backup_validate_get": "backup validate",
    "root__get": "info",
    "healthz_healthz_get": "ping",
}


@dataclass
class CliState:
    client: ApiClient
    json_output: bool


def client_factory(api_url: str, timeout: float) -> ApiClient:
    return ApiClient(api_url, timeout)


talk_app = typer.Typer(
    help="OPAL Talk text-to-speech (talks to the opal-talk-svc REST API).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
engines_app = typer.Typer(help="Discover and configure installed TTS engines.", no_args_is_help=True)
voices_app = typer.Typer(help="Register and manage host-resident voices.", no_args_is_help=True)
profiles_app = typer.Typer(help="Manage reusable generation profiles.", no_args_is_help=True)
settings_app = typer.Typer(help="Service-wide key/value settings.", no_args_is_help=True)
backup_app = typer.Typer(help="Backup, restore, import, export and validate configuration.", no_args_is_help=True)
for _sub, _name in (
    (engines_app, "engines"),
    (voices_app, "voices"),
    (profiles_app, "profiles"),
    (settings_app, "settings"),
    (backup_app, "backup"),
):
    talk_app.add_typer(_sub, name=_name)


@talk_app.callback()
def talk_callback(
    ctx: typer.Context,
    api_url: str = typer.Option(None, "--api-url", help="REST API base URL [env: OPAL_TALK_API_URL]"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON responses"),
    timeout: float = typer.Option(120.0, "--timeout", help="HTTP timeout in seconds"),
) -> None:
    ctx.obj = CliState(client_factory(api_url or api_url_from_env(), timeout), json_output)


# -- helpers ---------------------------------------------------------------------


def _state(ctx: typer.Context) -> CliState:
    return ctx.find_root().obj if not isinstance(ctx.obj, CliState) else ctx.obj


def _call(ctx: typer.Context, op: str, **kwargs: Any) -> httpx.Response:
    try:
        return _state(ctx).client.call(op, **kwargs)
    except ApiCallError as exc:
        typer.echo(f"Error: {exc.describe()}", err=True)
        raise typer.Exit(1) from None
    except httpx.HTTPError as exc:
        typer.echo(f"Error: cannot reach OPAL Talk API: {exc}", err=True)
        raise typer.Exit(2) from None


def _emit(ctx: typer.Context, data: Any, columns: list[str] | None = None) -> None:
    if columns and isinstance(data, list) and not _state(ctx).json_output:
        if not data:
            typer.echo("(none)")
            return
        rows = [[_cell(item.get(c)) for c in columns] for item in data]
        widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(columns)]
        typer.echo("  ".join(c.upper().ljust(w) for c, w in zip(columns, widths, strict=True)))
        for row in rows:
            typer.echo("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
        return
    typer.echo(json.dumps(data, indent=2))


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def _validation(ctx: typer.Context, resp: httpx.Response) -> None:
    data = resp.json()
    if _state(ctx).json_output:
        _emit(ctx, data)
    else:
        typer.echo("valid" if data["valid"] else "INVALID")
        for level in ("errors", "warnings"):
            for item in data[level]:
                where = f" [{item['field']}]" if item.get("field") else ""
                typer.echo(f"  {level[:-1]}: {item['code']}{where}: {item['message']}")
    if not data["valid"]:
        raise typer.Exit(1)


def _parse_options(pairs: list[str] | None, raw_json: str | None) -> dict[str, Any] | None:
    """Combine --options-json and repeated --option key=value (values parsed as JSON when possible)."""
    if not pairs and raw_json is None:
        return None
    options: dict[str, Any] = {}
    if raw_json is not None:
        try:
            loaded = json.loads(raw_json)
        except ValueError as exc:
            raise typer.BadParameter(f"invalid JSON: {exc}", param_hint="--options-json") from None
        if not isinstance(loaded, dict):
            raise typer.BadParameter("must be a JSON object", param_hint="--options-json")
        options.update(loaded)
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"expected key=value, got '{pair}'", param_hint="--option")
        try:
            options[key] = json.loads(value)
        except ValueError:
            options[key] = value
    return options


def _write_audio(resp: httpx.Response, output: str, streaming: bool = False) -> int:
    out = sys.stdout.buffer if output == "-" else open(output, "wb")  # noqa: SIM115
    total = 0
    try:
        chunks = resp.iter_bytes() if streaming else [resp.content]
        for chunk in chunks:
            out.write(chunk)
            out.flush()
            total += len(chunk)
    finally:
        if out is not sys.stdout.buffer:
            out.close()
    return total


def _audio_summary(resp: httpx.Response, output: str, size: int) -> None:
    h = resp.headers
    profile = f", profile={h['x-opal-profile-id']}" if "x-opal-profile-id" in h else ""
    typer.echo(
        f"wrote {size} bytes to {output} ({h.get('x-opal-audio-format')}, {h.get('x-opal-sample-rate')} Hz, "
        f"{h.get('x-opal-sample-format')}, voice={h.get('x-opal-voice-id')}, engine={h.get('x-opal-engine-id')}"
        f"{profile})",
        err=True,
    )


OptionPairs = typer.Option(None, "--option", "-O", help="Engine option key=value (repeatable; JSON values allowed)")
OptionsJson = typer.Option(None, "--options-json", help="Engine options as a JSON object")
Output = typer.Option(..., "--output", "-o", help="Output file ('-' for stdout)")

ENGINE_COLUMNS = ["engine_id", "name", "enabled", "available"]
VOICE_COLUMNS = ["voice_id", "name", "engine_id", "engine_voice_ref", "language", "enabled", "native_sample_rate"]
PROFILE_COLUMNS = ["id", "name", "is_active", "voice_id", "speed", "volume", "output_format", "sample_rate"]

# -- engines ---------------------------------------------------------------------


@engines_app.command("list")
def engines_list(ctx: typer.Context) -> None:
    """List installed engines."""
    _emit(ctx, _call(ctx, "list_engines_api_v1_engines__get").json(), ENGINE_COLUMNS)


@engines_app.command("get")
def engines_get(ctx: typer.Context, engine_id: str) -> None:
    """Show one engine."""
    _emit(ctx, _call(ctx, "get_engine_api_v1_engines__engine_id__get", path_params={"engine_id": engine_id}).json())


@engines_app.command("update")
def engines_update(
    ctx: typer.Context,
    engine_id: str,
    enabled: bool = typer.Option(None, "--enable/--disable", help="Enable or disable routing to the engine"),
    option: list[str] = OptionPairs,
    options_json: str = OptionsJson,
) -> None:
    """Update engine configuration (options are merged; a null value removes a key)."""
    body: dict[str, Any] = {}
    if enabled is not None:
        body["enabled"] = enabled
    options = _parse_options(option, options_json)
    if options is not None:
        body["options"] = options
    if not body:
        raise typer.BadParameter("nothing to update: use --enable/--disable or --option")
    resp = _call(ctx, "update_engine_api_v1_engines__engine_id__patch", path_params={"engine_id": engine_id}, json=body)
    _emit(ctx, resp.json())


@engines_app.command("validate")
def engines_validate(ctx: typer.Context, engine_id: str) -> None:
    """Validate an engine's runtime and configuration."""
    _validation(
        ctx, _call(ctx, "validate_engine_api_v1_engines__engine_id__validate_post", path_params={"engine_id": engine_id})
    )


# -- voices ----------------------------------------------------------------------


@voices_app.command("list")
def voices_list(
    ctx: typer.Context,
    engine: str = typer.Option(None, "--engine", help="Only voices of this engine"),
    enabled: bool = typer.Option(None, "--enabled/--disabled", help="Filter by enabled state"),
) -> None:
    """List registered voices."""
    resp = _call(ctx, "list_voices_api_v1_voices__get", params={"engine_id": engine,
                                                                 "enabled": None if enabled is None else str(enabled).lower()})
    _emit(ctx, resp.json(), VOICE_COLUMNS)


@voices_app.command("add")
def voices_add(
    ctx: typer.Context,
    voice_id: str,
    name: str = typer.Option(..., "--name", help="Human-readable name"),
    engine: str = typer.Option(..., "--engine", help="Engine that interprets the reference"),
    ref: str = typer.Option(..., "--ref", help="Opaque engine voice reference (e.g. model path or voice name)"),
    description: str = typer.Option(None, "--description"),
    language: str = typer.Option(None, "--language"),
    enabled: bool = typer.Option(True, "--enabled/--disabled"),
    option: list[str] = OptionPairs,
    options_json: str = OptionsJson,
) -> None:
    """Register a voice resource already installed on the host."""
    body: dict[str, Any] = {"voice_id": voice_id, "name": name, "engine_id": engine, "engine_voice_ref": ref,
                            "enabled": enabled, "engine_options": _parse_options(option, options_json) or {}}
    if description is not None:
        body["description"] = description
    if language is not None:
        body["language"] = language
    _emit(ctx, _call(ctx, "create_voice_api_v1_voices__post", json=body).json())


@voices_app.command("get")
def voices_get(ctx: typer.Context, voice_id: str) -> None:
    """Show one voice."""
    _emit(ctx, _call(ctx, "get_voice_api_v1_voices__voice_id__get", path_params={"voice_id": voice_id}).json())


@voices_app.command("update")
def voices_update(
    ctx: typer.Context,
    voice_id: str,
    name: str = typer.Option(None, "--name"),
    engine: str = typer.Option(None, "--engine"),
    ref: str = typer.Option(None, "--ref"),
    description: str = typer.Option(None, "--description"),
    clear_description: bool = typer.Option(False, "--clear-description"),
    language: str = typer.Option(None, "--language"),
    clear_language: bool = typer.Option(False, "--clear-language"),
    enabled: bool = typer.Option(None, "--enabled/--disabled"),
    option: list[str] = OptionPairs,
    options_json: str = OptionsJson,
) -> None:
    """Update a voice registration (engine_options are replaced)."""
    body: dict[str, Any] = {}
    for key, value in (("name", name), ("engine_id", engine), ("engine_voice_ref", ref), ("description", description),
                       ("language", language), ("enabled", enabled)):
        if value is not None:
            body[key] = value
    if clear_description:
        body["description"] = None
    if clear_language:
        body["language"] = None
    options = _parse_options(option, options_json)
    if options is not None:
        body["engine_options"] = options
    if not body:
        raise typer.BadParameter("nothing to update")
    resp = _call(ctx, "update_voice_api_v1_voices__voice_id__patch", path_params={"voice_id": voice_id}, json=body)
    _emit(ctx, resp.json())


@voices_app.command("remove")
def voices_remove(ctx: typer.Context, voice_id: str) -> None:
    """Unregister a voice (host model files are never touched)."""
    _call(ctx, "delete_voice_api_v1_voices__voice_id__delete", path_params={"voice_id": voice_id})
    typer.echo(f"voice '{voice_id}' unregistered")


@voices_app.command("validate")
def voices_validate(ctx: typer.Context, voice_id: str) -> None:
    """Validate that a voice registration resolves and is usable."""
    _validation(
        ctx, _call(ctx, "validate_voice_api_v1_voices__voice_id__validate_post", path_params={"voice_id": voice_id})
    )


@voices_app.command("test")
def voices_test(
    ctx: typer.Context,
    voice_id: str,
    output: str = Output,
    text: str = typer.Option(None, "--text", help="Test phrase (service default if omitted)"),
    speed: float = typer.Option(None, "--speed", min=0.25, max=4.0),
    volume: float = typer.Option(None, "--volume", min=0.0, max=2.0),
    output_format: str = typer.Option(None, "--format", help="wav or pcm"),
    sample_rate: int = typer.Option(None, "--sample-rate", min=8000, max=192000),
    option: list[str] = OptionPairs,
    options_json: str = OptionsJson,
) -> None:
    """Generate a short test utterance with a voice."""
    body: dict[str, Any] = {}
    for key, value in (("text", text), ("speed", speed), ("volume", volume), ("output_format", output_format),
                       ("sample_rate", sample_rate)):
        if value is not None:
            body[key] = value
    options = _parse_options(option, options_json)
    if options is not None:
        body["engine_options"] = options
    resp = _call(ctx, "test_voice_api_v1_voices__voice_id__test_post", path_params={"voice_id": voice_id},
                 json=body or None)
    _audio_summary(resp, output, _write_audio(resp, output))


# -- profiles --------------------------------------------------------------------


def _profile_body(
    name: str | None, description: str | None, voice: str | None, speed: float | None, volume: float | None,
    output_format: str | None, sample_rate: int | None, active: bool | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key, value in (("name", name), ("description", description), ("voice_id", voice), ("speed", speed),
                       ("volume", volume), ("output_format", output_format), ("sample_rate", sample_rate),
                       ("is_active", active)):
        if value is not None:
            body[key] = value
    return body


@profiles_app.command("list")
def profiles_list(ctx: typer.Context) -> None:
    """List profiles."""
    _emit(ctx, _call(ctx, "list_profiles_api_v1_profiles__get").json(), PROFILE_COLUMNS)


@profiles_app.command("add")
def profiles_add(
    ctx: typer.Context,
    name: str,
    voice: str = typer.Option(..., "--voice", help="Registered voice_id"),
    description: str = typer.Option(None, "--description"),
    speed: float = typer.Option(None, "--speed", min=0.25, max=4.0),
    volume: float = typer.Option(None, "--volume", min=0.0, max=2.0),
    output_format: str = typer.Option(None, "--format", help="wav or pcm"),
    sample_rate: int = typer.Option(None, "--sample-rate", min=8000, max=192000),
    active: bool = typer.Option(False, "--active", help="Activate this profile"),
    option: list[str] = OptionPairs,
    options_json: str = OptionsJson,
) -> None:
    """Create a profile."""
    body = _profile_body(name, description, voice, speed, volume, output_format, sample_rate, active)
    body["engine_options"] = _parse_options(option, options_json) or {}
    _emit(ctx, _call(ctx, "create_profile_api_v1_profiles__post", json=body).json())


@profiles_app.command("get")
def profiles_get(ctx: typer.Context, profile_id: int) -> None:
    """Show one profile."""
    _emit(ctx, _call(ctx, "get_profile_api_v1_profiles__profile_id__get",
                     path_params={"profile_id": profile_id}).json())


@profiles_app.command("update")
def profiles_update(
    ctx: typer.Context,
    profile_id: int,
    name: str = typer.Option(None, "--name"),
    voice: str = typer.Option(None, "--voice"),
    description: str = typer.Option(None, "--description"),
    clear_description: bool = typer.Option(False, "--clear-description"),
    speed: float = typer.Option(None, "--speed", min=0.25, max=4.0),
    volume: float = typer.Option(None, "--volume", min=0.0, max=2.0),
    output_format: str = typer.Option(None, "--format", help="wav or pcm"),
    sample_rate: int = typer.Option(None, "--sample-rate", min=8000, max=192000),
    native_rate: bool = typer.Option(False, "--native-rate", help="Clear sample_rate (service chooses)"),
    active: bool = typer.Option(None, "--active/--inactive"),
    option: list[str] = OptionPairs,
    options_json: str = OptionsJson,
) -> None:
    """Partially update a profile (engine_options are replaced)."""
    body = _profile_body(name, description, voice, speed, volume, output_format, sample_rate, active)
    if clear_description:
        body["description"] = None
    if native_rate:
        body["sample_rate"] = None
    options = _parse_options(option, options_json)
    if options is not None:
        body["engine_options"] = options
    if not body:
        raise typer.BadParameter("nothing to update")
    resp = _call(ctx, "update_profile_api_v1_profiles__profile_id__patch", path_params={"profile_id": profile_id},
                 json=body)
    _emit(ctx, resp.json())


@profiles_app.command("remove")
def profiles_remove(ctx: typer.Context, profile_id: int) -> None:
    """Delete a (non-active) profile."""
    _call(ctx, "delete_profile_api_v1_profiles__profile_id__delete", path_params={"profile_id": profile_id})
    typer.echo(f"profile {profile_id} deleted")


@profiles_app.command("activate")
def profiles_activate(ctx: typer.Context, profile_id: int) -> None:
    """Make a profile the active one."""
    resp = _call(ctx, "activate_profile_api_v1_profiles__profile_id__activate_post",
                 path_params={"profile_id": profile_id})
    _emit(ctx, resp.json())


# -- generate --------------------------------------------------------------------


@talk_app.command("generate")
def generate(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Plain text to speak ('-' reads stdin)"),
    output: str = Output,
    stream: bool = typer.Option(False, "--stream", help="Use /generate/stream (progressive raw PCM)"),
    profile: int = typer.Option(None, "--profile", min=1, help="Profile id (default: active profile)"),
    voice: str = typer.Option(None, "--voice"),
    engine: str = typer.Option(None, "--engine", help="Advanced engine override"),
    speed: float = typer.Option(None, "--speed", min=0.25, max=4.0),
    volume: float = typer.Option(None, "--volume", min=0.0, max=2.0),
    output_format: str = typer.Option(None, "--format", help="wav or pcm (not with --stream)"),
    sample_rate: int = typer.Option(None, "--sample-rate", min=8000, max=192000),
    option: list[str] = OptionPairs,
    options_json: str = OptionsJson,
) -> None:
    """Generate speech. With --stream, raw S16LE mono PCM is written as it arrives.

    Example: opal talk generate "Hello" --stream -o - | aplay -r 22050 -f S16_LE -c 1
    """
    if text == "-":
        text = sys.stdin.read()
    if stream and output_format is not None:
        raise typer.BadParameter("--format is not supported with --stream (always pcm)")
    body: dict[str, Any] = {"text": text}
    for key, value in (("profile_id", profile), ("voice_id", voice), ("engine_id", engine), ("speed", speed),
                       ("volume", volume), ("output_format", output_format), ("sample_rate", sample_rate)):
        if value is not None:
            body[key] = value
    options = _parse_options(option, options_json)
    if options is not None:
        body["engine_options"] = options

    if not stream:
        resp = _call(ctx, "generate_audio_api_v1_generate_post", json=body)
        _audio_summary(resp, output, _write_audio(resp, output))
        return
    try:
        with _state(ctx).client.stream("stream_audio_api_v1_generate_stream_post", json=body) as resp:
            size = _write_audio(resp, output, streaming=True)
    except ApiCallError as exc:
        typer.echo(f"Error: {exc.describe()}", err=True)
        raise typer.Exit(1) from None
    except httpx.HTTPError as exc:
        typer.echo(f"Error: stream terminated prematurely; generation failed ({exc})", err=True)
        raise typer.Exit(1) from None
    _audio_summary(resp, output, size)


# -- settings --------------------------------------------------------------------


@settings_app.command("list")
def settings_list(ctx: typer.Context) -> None:
    """Show all stored settings."""
    _emit(ctx, _call(ctx, "get_all_settings_api_v1_settings__get").json())


@settings_app.command("get")
def settings_get(ctx: typer.Context, key: str) -> None:
    """Show one setting."""
    _emit(ctx, _call(ctx, "get_setting_api_v1_settings__key__get", path_params={"key": key}).json())


@settings_app.command("set")
def settings_set(ctx: typer.Context, key: str, value: str) -> None:
    """Create or replace a setting."""
    _emit(ctx, _call(ctx, "upsert_setting_api_v1_settings__key__put", path_params={"key": key},
                     json={"value": value}).json())


@settings_app.command("delete")
def settings_delete(ctx: typer.Context, key: str) -> None:
    """Delete a setting so the default applies."""
    _call(ctx, "delete_setting_api_v1_settings__key__delete", path_params={"key": key})
    typer.echo(f"setting '{key}' deleted")


# -- health / identity -------------------------------------------------------------


@talk_app.command("health")
def health(ctx: typer.Context) -> None:
    """Service readiness and engine state."""
    data = _call(ctx, "health_check_api_v1_health__get").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    typer.echo(f"status: {data['status']}  version: {data['version']}  active_profile_id: {data.get('active_profile_id')}")
    _emit(ctx, data["engines"], ["engine_id", "enabled", "available", "message"])


@talk_app.command("info")
def info(ctx: typer.Context) -> None:
    """Service identity and API version."""
    _emit(ctx, _call(ctx, "root__get").json())


@talk_app.command("ping")
def ping(ctx: typer.Context) -> None:
    """Liveness check."""
    typer.echo(_call(ctx, "healthz_healthz_get").json()["status"])


# -- backup ----------------------------------------------------------------------


@backup_app.command("list")
def backup_list(ctx: typer.Context) -> None:
    """List backups."""
    _emit(ctx, _call(ctx, "list_backups_api_v1_backup_list_get").json(), ["filename", "created_at", "size_bytes"])


@backup_app.command("create")
def backup_create(ctx: typer.Context) -> None:
    """Create a configuration backup."""
    _emit(ctx, _call(ctx, "create_backup_api_v1_backup_create_post").json())


@backup_app.command("download")
def backup_download(
    ctx: typer.Context,
    filename: str,
    output: str = typer.Option(None, "--output", "-o", help="Destination (default: ./FILENAME, '-' for stdout)"),
) -> None:
    """Download a backup file."""
    resp = _call(ctx, "download_backup_api_v1_backup_download__filename__get", path_params={"filename": filename})
    target = output or str(Path(filename).name)
    size = _write_audio(resp, target)
    typer.echo(f"wrote {size} bytes to {target}", err=True)


@backup_app.command("restore")
def backup_restore(ctx: typer.Context, filename: str) -> None:
    """Restore configuration from a backup."""
    _emit(ctx, _call(ctx, "restore_backup_api_v1_backup_restore__filename__post",
                     path_params={"filename": filename}).json())


@backup_app.command("delete")
def backup_delete(ctx: typer.Context, filename: str) -> None:
    """Delete a backup."""
    _call(ctx, "delete_backup_api_v1_backup__filename__delete", path_params={"filename": filename})
    typer.echo(f"backup '{filename}' deleted")


@backup_app.command("export")
def backup_export(
    ctx: typer.Context, output: str = typer.Option(None, "--output", "-o", help="Write JSON to a file")
) -> None:
    """Export the full logical configuration as JSON."""
    data = _call(ctx, "export_config_api_v1_backup_export_get").json()
    if output:
        Path(output).write_text(json.dumps(data, indent=2), encoding="utf-8")
        typer.echo(f"exported configuration to {output}", err=True)
    else:
        typer.echo(json.dumps(data, indent=2))


@backup_app.command("import")
def backup_import(ctx: typer.Context) -> None:
    """Import configuration from the service's ./config/ YAML files."""
    data = _call(ctx, "import_config_api_v1_backup_import_post").json()
    _emit(ctx, data)
    if not data["success"]:
        raise typer.Exit(1)


@backup_app.command("validate")
def backup_validate(ctx: typer.Context) -> None:
    """Validate the current configuration."""
    _validation(ctx, _call(ctx, "validate_config_api_v1_backup_validate_get"))


# -- `opal` root -----------------------------------------------------------------

app = typer.Typer(help="OPAL command line.", no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(talk_app, name="talk")

try:  # Share the `opal` root with the sister OPAL Listen CLI when it is installed.
    from opal_listener.cli.main import listen_app  # type: ignore[import-not-found]

    app.add_typer(listen_app, name="listen")
except Exception:  # noqa: BLE001
    pass


if __name__ == "__main__":
    app()
