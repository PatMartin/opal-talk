# Real-engine release validation

`tests/test_release_validation.py` verifies OPAL Talk 1.2.0 against actual
Piper and Kokoro engines: engine availability and model initialization, WAV
generation, PCM streaming, output format headers, model caching across
requests, configuration updates between requests, and performance measurement
(time to first audio, total duration, allocation peak).

These tests are skipped automatically when the runtime or model files are
missing, so they never run — or fail — in standard CI
(`pytest -m "not piper and not kokoro"`). No models are downloaded by CI.

## Prerequisites

```sh
pip install -e '.[piper]'     # piper-tts + onnxruntime
pip install -e '.[kokoro]'    # kokoro-onnx
pip install -e '.[engines]'   # both
```

## Model files

| Variable | Meaning | Default |
|---|---|---|
| `OPAL_TALK_TEST_PIPER_MODEL` | Path to a Piper voice `.onnx`; its `.onnx.json` config must sit alongside | `~/piper/voices/en_US-amy-low.onnx` |
| `OPAL_TALK_TEST_KOKORO_DIR` | Directory containing `kokoro-v1.0*.onnx` and `voices-v1.0.bin` | `~/kokoro` |

Piper voices are published on the `rhasspy/piper-voices` Hugging Face repo —
download the `.onnx` and `.onnx.json` for one voice (e.g. `en_US-amy-low`).
Kokoro release archives provide `kokoro-v1.0.onnx` and `voices-v1.0.bin`.

## Running

```sh
# Linux / macOS
OPAL_TALK_TEST_PIPER_MODEL=~/piper/voices/en_US-amy-low.onnx pytest -m piper
OPAL_TALK_TEST_KOKORO_DIR=~/kokoro pytest -m kokoro
pytest -m "piper or kokoro"        # whichever is available
pytest tests/test_release_validation.py -v -s   # -s prints the measurements
```

```powershell
# Windows (PowerShell)
$env:OPAL_TALK_TEST_PIPER_MODEL="$HOME\piper\voices\en_US-amy-low.onnx"
pytest -m piper
$env:OPAL_TALK_TEST_KOKORO_DIR="$HOME\kokoro"
pytest -m kokoro
```

```sh
# Raspberry Pi (aarch64): piper-tts publishes ARM wheels; prefer a
# `low`-quality voice. Kokoro is practical only on 64-bit systems.
OPAL_TALK_TEST_PIPER_MODEL=~/piper/voices/en_US-amy-low.onnx pytest -m piper
```

Timing and memory figures are printed for the operator's review (`-s`);
assertions stay relative and hardware-independent on purpose.

## Standard CI

Regular push/PR runs use only the fake engine:

```sh
pytest -m "not piper and not kokoro"
```

The `release-validation` workflow job is `workflow_dispatch`-only: it downloads
the small public `en_US-amy-low` Piper voice and runs `-m piper` on demand.
It never runs on push or pull requests.
