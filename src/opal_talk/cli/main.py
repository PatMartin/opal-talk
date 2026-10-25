"""`opal talk ...` command line. Every command calls the OPAL Talk REST API (see COMMANDS)."""

from __future__ import annotations

import typer

from opal_talk.cli.client import api_url_from_env
from opal_talk.cli.commands import backup, engines, generate, profiles, root, settings, voices
from opal_talk.cli.common import CliState, client_factory  # noqa: F401 - re-exported; tests patch main.client_factory

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
    "get_talk_health_live": "health --live",
    "get_talk_health_ready": "health --ready",
    "patch_talk_settings": "settings patch",
    "validate_talk_backup_payload": "backup validate --payload",
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


talk_app = typer.Typer(
    help="OPAL Talk text-to-speech (talks to the opal-talk-svc REST API).",
    epilog="Global options (--api-url, --json, --timeout) go before the command group, "
           "e.g. 'opal talk --json voices list'.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@talk_app.callback()
def talk_callback(
    ctx: typer.Context,
    api_url: str = typer.Option(None, "--api-url", help="REST API base URL [env: OPAL_TALK_API_URL]"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON responses"),
    timeout: float = typer.Option(120.0, "--timeout", help="HTTP timeout in seconds"),
) -> None:
    ctx.obj = CliState(client_factory(api_url or api_url_from_env(), timeout), json_output)


for _module in (engines, voices, profiles, generate, settings, backup, root):
    _module.register(talk_app)


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
