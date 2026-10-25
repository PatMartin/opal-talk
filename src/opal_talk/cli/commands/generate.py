"""`opal talk generate` — completed and streamed speech generation."""

from __future__ import annotations

import sys
from typing import Any

import httpx
import typer

from opal_talk.cli.client import ApiCallError
from opal_talk.cli.common import (
    OptionPairs,
    OptionsJson,
    Output,
    _audio_summary,
    _call,
    _parse_options,
    _state,
    _write_audio,
)


def register(talk_app: typer.Typer) -> None:
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
