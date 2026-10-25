"""`opal talk profiles ...` — manage reusable generation profiles."""

from __future__ import annotations

from typing import Any

import typer

from opal_talk.cli.common import PROFILE_COLUMNS, OptionPairs, OptionsJson, _call, _emit, _parse_options

profiles_app = typer.Typer(help="Manage reusable generation profiles.", no_args_is_help=True)


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


def register(talk_app: typer.Typer) -> None:
    talk_app.add_typer(profiles_app, name="profiles")
