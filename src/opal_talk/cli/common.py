"""Shared state, HTTP helpers and output formatting for `opal talk` commands."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx
import typer

from opal_talk.cli.client import ApiCallError, ApiClient


@dataclass
class CliState:
    client: ApiClient
    json_output: bool


def client_factory(api_url: str, timeout: float) -> ApiClient:
    return ApiClient(api_url, timeout)


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
