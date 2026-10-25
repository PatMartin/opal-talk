"""Root-level commands: health, service info and liveness ping."""

from __future__ import annotations

import typer

from opal_talk.cli.common import _call, _emit, _state


def register(talk_app: typer.Typer) -> None:
    @talk_app.command("health")
    def health(ctx: typer.Context, live: bool = typer.Option(False, "--live", help="Liveness probe."),
               ready: bool = typer.Option(False, "--ready", help="Readiness probe.")) -> None:
        """Service readiness and engine state."""
        if live:
            _emit(ctx, _call(ctx, "get_talk_health_live").json())
            return
        if ready:
            from opal_talk.cli.client import ApiCallError

            try:
                _emit(ctx, _call(ctx, "get_talk_health_ready").json())
            except ApiCallError as e:
                if e.status != 503:
                    raise
                _emit(ctx, e.body)
            return
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
