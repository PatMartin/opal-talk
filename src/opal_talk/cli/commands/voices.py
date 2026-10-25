"""`opal talk voices ...` — register and manage host-resident voices."""

from __future__ import annotations

from typing import Any

import typer

from opal_talk.cli.common import (
    VOICE_COLUMNS,
    OptionPairs,
    OptionsJson,
    Output,
    _audio_summary,
    _call,
    _emit,
    _parse_options,
    _validation,
    _write_audio,
)

voices_app = typer.Typer(help="Register and manage host-resident voices.", no_args_is_help=True)


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


def register(talk_app: typer.Typer) -> None:
    talk_app.add_typer(voices_app, name="voices")
