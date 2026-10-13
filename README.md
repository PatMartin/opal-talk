# OPAL Talk

Implementation-neutral text-to-speech for the OPAL suite. The authoritative REST contract is [opal_talk.yaml](opal_talk.yaml).

- **`opal-talk-svc`** – FastAPI service implementing every contract operation, with working **Piper** and **Kokoro** adapters, WAV/PCM output, progressive raw-PCM streaming and internal text chunking. No playback.
- **`opal talk …`** – Typer CLI; every command calls the REST API.
- **SPA** – React UI in [spa/](spa/) that talks only to the REST API.

Interpretations of under-specified contract details are listed in [docs/CONTRACT_NOTES.md](docs/CONTRACT_NOTES.md). The [User Guide](docs/USER_GUIDE.md) covers installation, architecture, configuration, the REST API, streaming, the web GUI and integration; the complete CLI reference, with examples, is [docs/CLI.md](docs/CLI.md).

## Architecture

```
src/opal_talk/
  api/            REST layer (routes use the contract's operationIds; /openapi.json serves opal_talk.yaml)
  service/        application layer: catalog (engines/voices/profiles), generation, settings, config/backup
  tts/            engine-neutral core: TtsEngine interface, chunking, resampling/gain/WAV encoding, registry
    piper_adapter.py   Piper (piper-tts)
    kokoro_adapter.py  Kokoro (kokoro-onnx)
  persistence/    SQLite store
  cli/            HTTP client + Typer commands
  config.py       YAML service configuration
spa/              React + Vite + Vitest
config/           service.yaml and importable engines/voices/profiles/settings YAML
```

Adapters implement `TtsEngine` (`availability`, `describe_voice`, `synthesize`, declarative `option_specs`). The core handles chunking, sample-rate conversion, volume and encoding, so engine-specific behaviour stays in the adapter and is reached only through opaque `engine_options`. Further engines can be registered under the `opal_talk.engines` entry-point group.

## Install

Requires Python ≥ 3.11 and (for the SPA) Node ≥ 18.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[engines,dev]'   # or '.[piper]' / '.[kokoro]' / '.' for core only
(cd spa && npm install && npm run build)      # optional: served at /ui
```

### Engine resources (host-installed, never uploaded)

- **Piper**: `*.onnx` voice + `*.onnx.json` config. Set `voices_dir` so voices can be referenced by name (`en_US-amy-low`), or register an absolute model path.
- **Kokoro**: `kokoro-v1.0.onnx` (or `.int8.onnx`) and `voices-v1.0.bin` from the kokoro-onnx releases; set `model_path` and `voices_path`. Voice references are Kokoro voice names (`af_heart`, `bm_george`, …).

## Configure

- `config/service.yaml` – host/port (default `127.0.0.1:8001`), `data_dir` (SQLite + backups, default `./var`), `config_dir`, CORS origins, SPA dir and initial engine options. Override the file with `OPAL_TALK_CONFIG`; override fields with `OPAL_TALK_HOST`, `OPAL_TALK_PORT`, `OPAL_TALK_DATA_DIR`, `OPAL_TALK_CONFIG_DIR`, `OPAL_TALK_SPA_DIR`.
- `config/{engines,voices,profiles,settings}.yaml` – imported on first start and by `POST /api/v1/backup/import` (`opal talk backup import`). Only files that are present replace their section; everything is validated before anything is applied.
- Settings (`opal talk settings set KEY VALUE`): `max_text_chars` (413 above), `chunk_max_chars`, `default_speed`, `default_volume`, `default_output_format`.

## Run

```bash
.venv/bin/opal-talk-svc                      # http://127.0.0.1:8001, docs at /docs, SPA at /ui
export OPAL_TALK_API_URL=http://127.0.0.1:8001   # CLI default

opal talk health
opal talk engines list
opal talk voices add lessac --name Lessac --engine piper --ref en_US-lessac-medium
opal talk profiles add main --voice lessac --active
opal talk generate "Hello from OPAL Talk." -o hello.wav
opal talk generate "Streamed speech." --stream -o - | aplay -r 22050 -f S16_LE -c 1
opal talk backup create
```

`opal-talk …` is equivalent to `opal talk …`. Add `--json` for raw responses; `-O key=value` / `--options-json` pass `engine_options`.

## Development

```bash
make test        # Python: unit, contract, adapter (real Piper/Kokoro when present), CLI, integration, streaming
make test-fast   # without real engines
make test-spa    # Vitest + typecheck
make lint
make spa-dev     # Vite dev server on :5173 against the service on :8001
```

Real-engine tests look for `~/piper/voices/en_US-amy-low.onnx` and `~/kokoro/` (override with `OPAL_TALK_TEST_PIPER_MODEL`, `OPAL_TALK_TEST_KOKORO_DIR`) and are skipped when absent.

Parity is enforced by tests: every contract operation has a route with the same operationId/method/path, every response in the suite is validated against `opal_talk.yaml`, the CLI and SPA operation maps equal the contract, every operation has a CLI command that is exercised, and every operation is used by the SPA.
