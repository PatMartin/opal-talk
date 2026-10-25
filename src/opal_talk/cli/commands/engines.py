"""`opal talk engines ...` — discover and configure installed TTS engines."""

from __future__ import annotations

from typing import Any

import typer

from opal_talk.cli.common import ENGINE_COLUMNS, OptionPairs, OptionsJson, _call, _emit, _parse_options, _validation

engines_app = typer.Typer(help="Discover and configure installed TTS engines.", no_args_is_help=True)


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


def register(talk_app: typer.Typer) -> None:
    talk_app.add_typer(engines_app, name="engines")
