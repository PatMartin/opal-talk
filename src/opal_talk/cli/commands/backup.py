"""`opal talk backup ...` — backup, restore, import, export and validate configuration."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from opal_talk.cli.common import _call, _emit, _validation, _write_audio

backup_app = typer.Typer(help="Backup, restore, import, export and validate configuration.", no_args_is_help=True)


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


def register(talk_app: typer.Typer) -> None:
    talk_app.add_typer(backup_app, name="backup")
