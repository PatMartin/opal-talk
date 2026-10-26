# Performance & acceptance tests

`tests/test_performance.py` measures generation behavior end to end: time to
first audio byte, total synthesis duration, streaming throughput, peak
intermediate buffer usage, chunk ordering, long-text generation and concurrent
generation under bounded inference capacity.

All timing assertions are relative — ratios of work rather than absolute
seconds — so the suite stays honest on slow CI runners, fast workstations and
embedded hosts alike. There are no hardcoded millisecond thresholds.

## Deterministic tests (normal CI)

The fake tone engine drives every measurement, so these tests run everywhere
with no external dependencies:

```sh
pytest tests/test_performance.py            # just this file
make test-fast                              # whole suite without real engines
```

## Real-engine smoke tests

`test_piper_stream_smoke` and `test_kokoro_stream_smoke` exercise a live
`/generate/stream` request against the real adapters. They are skipped unless
the runtimes *and* model files are present, so a standard `pytest` / `make test`
run never fails on a machine that lacks them.

### Installing the runtimes

```sh
pip install -e '.[piper]'     # piper-tts + onnxruntime
pip install -e '.[kokoro]'    # kokoro-onnx
pip install -e '.[engines]'   # both
```

### Pointing the tests at model files

| Variable                      | Meaning                                                              |
|-------------------------------|----------------------------------------------------------------------|
| `OPAL_TALK_TEST_PIPER_MODEL`  | Path to a Piper voice `.onnx` (its `.onnx.json` config must sit alongside). Default: `~/piper/voices/en_US-amy-low.onnx` |
| `OPAL_TALK_TEST_KOKORO_DIR`   | Directory containing `kokoro-v1.0*.onnx` and `voices-v1.0.bin`. Default: `~/kokoro` |

```sh
OPAL_TALK_TEST_PIPER_MODEL=/models/en_US-amy-low.onnx pytest -m piper
OPAL_TALK_TEST_KOKORO_DIR=/models/kokoro pytest -m kokoro
pytest -m "piper or kokoro"    # run whichever is available
```

### CPU hosts

Nothing extra is needed — ONNX Runtime uses the CPU provider by default and the
Piper engine's `use_cuda` option defaults to false.

### GPU hosts (CUDA)

Install the CUDA-enabled ONNX Runtime build (`onnxruntime-gpu`) and set the
Piper engine option `use_cuda: true` (via `config/service.yaml` or
`PATCH /api/v1/engines/piper`) before running the smoke test. Timing
assertions are relative, so GPU speedups cannot trip them. Kokoro runs on CPU
ONNX regardless.

### Raspberry Pi

piper-tts ships aarch64 wheels; piwheels supplies onnxruntime for common Pi
OS images. Prefer a `low`-quality voice (e.g. `en_US-amy-low`) and pass its path
through `OPAL_TALK_TEST_PIPER_MODEL`. Everything else is identical:

```sh
pip install -e '.[piper]'
OPAL_TALK_TEST_PIPER_MODEL=~/piper/voices/en_US-amy-low.onnx pytest -m piper
```
