# `opal talk` CLI Reference

`opal talk` is the terminal interface to the OPAL Talk text-to-speech service (`opal-talk-svc`). Use it
to discover and configure TTS engines, register voices, manage profiles and settings, generate speech
(as a finished file or as a live raw-PCM stream), and back up, restore, import, export and validate the
configuration.

The CLI is a thin REST client. **Every command calls the OPAL Talk REST API.** It never opens the
database or loads an engine itself, so the service must be running, either locally or on another
machine. The CLI never plays audio either. It writes audio to a file or to stdout, and you pipe it into a
player such as `aplay`.

---

## Table of contents

1. [Installation and invocation](#1-installation-and-invocation)
2. [Connecting to the service](#2-connecting-to-the-service)
3. [Conventions](#3-conventions)
4. [Command tree](#4-command-tree)
5. [`engines`: engine discovery and configuration](#5-engines-engine-discovery-and-configuration)
6. [`voices`: voice registrations](#6-voices-voice-registrations)
7. [`profiles`: generation profiles](#7-profiles-generation-profiles)
8. [`generate`: speech generation and streaming](#8-generate-speech-generation-and-streaming)
9. [`settings`: service settings](#9-settings-service-settings)
10. [`backup`: backup, restore, import, export and validation](#10-backup-backup-restore-import-export-and-validation)
11. [`health`, `info`, `ping`: service status](#11-health-info-ping-service-status)
12. [Engine options reference](#12-engine-options-reference)
13. [Walkthroughs](#13-walkthroughs)
14. [Scripting and automation](#14-scripting-and-automation)
15. [Troubleshooting](#15-troubleshooting)
16. [Appendix: CLI ↔ REST mapping](#16-appendix-cli--rest-mapping)

---

## 1. Installation and invocation

Installing the project installs the CLI together with the service:

```bash
cd /gh/opal/opal-talk
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[engines,dev]'    # or '.[piper]', '.[kokoro]', or '.' for the core only
# or simply: make install
```

This installs three console scripts:

| Command | Purpose |
|---|---|
| `opal-talk-svc` | Runs the REST service (same as `python -m opal_talk`) |
| `opal-talk` | The CLI. Always unambiguous: `opal-talk <group> <command>` |
| `opal` | Shared OPAL root: `opal talk <group> <command>` |

You can invoke the CLI in three equivalent ways:

```bash
opal talk --help                              # shared OPAL root
opal-talk --help                              # standalone alias, identical to 'opal talk'
python -m opal_talk.cli.main talk --help      # same CLI, run as a module
```

> **`opal` and OPAL Listen.** OPAL Listen also installs an `opal` console script. If both packages are
> installed in the same environment, whichever was installed last owns `opal`. The `opal` from this
> package mounts `opal listen` as well when `opal_listener` is importable. `opal-talk` always works, so
> prefer it in scripts. Everything in this document applies to both forms. Examples use `opal talk`.

Start the service before you run any command:

```bash
opal-talk-svc                     # or: python -m opal_talk, or: make run
opal-talk-svc --port 8001 --host 0.0.0.0 --config /etc/opal-talk/service.yaml
```

### Getting help

`--help` works at every level of the command tree:

```bash
opal --help                        # top level (the 'talk' group, plus 'listen' if installed)
opal talk --help                   # global options and all command groups
opal talk voices --help            # commands in one group
opal talk voices add --help        # arguments and options of one command
```

Running a group without a command also prints its help:

```bash
opal talk profiles                 # same as: opal talk profiles --help
```

### Shell completion

The CLI is built with Typer, which provides tab completion:

```bash
opal --install-completion bash     # or zsh / fish; restart the shell afterwards
opal-talk --install-completion bash
opal --show-completion bash        # print the script instead of installing it
```

---

## 2. Connecting to the service

### Global options

Global options belong to the `talk` group. Put them **after `talk` (or `opal-talk`) and before the
command group**:

```
opal talk [--api-url URL] [--json] [--timeout SECONDS] <group> <command> [ARGS] [OPTIONS]
opal-talk [--api-url URL] [--json] [--timeout SECONDS] <group> <command> [ARGS] [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--api-url URL` | `$OPAL_TALK_API_URL`, otherwise `http://127.0.0.1:8001` | Base URL of the REST API |
| `--json` | off | Print raw API JSON instead of tables and messages |
| `--timeout SECONDS` | `120` | HTTP timeout for each request. Long texts and first-time model loads can take a while. |

```bash
# Correct: global options come right after 'talk'
opal talk --json voices list
opal talk --api-url http://pi5.local:8001 --timeout 300 health

# Wrong: --json after the group is rejected as an unknown option (exit code 2)
opal talk voices list --json
```

### Environment variables

Set `OPAL_TALK_API_URL` once so you don't have to pass `--api-url` on every call:

```bash
export OPAL_TALK_API_URL=http://192.168.1.50:8001
opal talk ping                     # talks to 192.168.1.50

# One-off override for a single command
OPAL_TALK_API_URL=http://pi5.local:8001 opal talk health

# --api-url takes precedence over the environment variable
opal talk --api-url http://127.0.0.1:8001 info
```

> The default port is **8001**, so OPAL Talk and OPAL Listen (port 8000) can run side by side on one
> host.

### Checking the connection

```bash
opal talk ping                     # ok
opal talk info                     # service name, version and API version
opal talk health                   # readiness and engine state
```

If the service cannot be reached, the CLI prints the connection error and exits with code `2`:

```
Error: cannot reach OPAL Talk API: [Errno 111] Connection refused
```

---

## 3. Conventions

### Referencing resources

| Resource | Referenced by | Example |
|---|---|---|
| Engine | `engine_id`, fixed by the adapter | `piper`, `kokoro` |
| Voice | `voice_id`, chosen when you register it | `en-us-amy-low`, `kokoro-af-heart` |
| Profile | Numeric `id` **only** (not the name) | `opal talk profiles get 3` |
| Setting | Key | `max_text_chars` |
| Backup | Filename | `opal-talk-2026-10-12T162205Z.tar.gz` |

Voice IDs must match `^[A-Za-z0-9][A-Za-z0-9._-]*$` (at most 200 characters): a letter or digit, followed
by letters, digits, `.`, `_` or `-`.

> Profiles are addressed by id, as in the REST API. Find the id with `opal talk profiles list`, or in a
> script with `opal talk --json profiles list | jq '.[] | select(.name=="narrator") | .id'`.

### Output formats

By default the CLI prints human-friendly output:

| Command kind | Default output |
|---|---|
| `list` (engines, voices, profiles, backups) | A table with the most important columns. An empty list prints `(none)`. |
| `get`, `add`, `update`, `activate`, `info`, `settings list`, `backup create/restore/import` | Pretty-printed JSON of the API response |
| `remove`, `delete` | A one-line confirmation, e.g. `voice 'test-voice' unregistered` |
| `validate` (engines, voices, backup) | `valid` or `INVALID`, followed by one line per error and warning |
| `health` | A status line, then an engine table |
| `generate`, `voices test` | Audio goes to the `-o` target. A summary line goes to **stderr**. |

In tables, `null` values are shown as `-`, booleans as `yes`/`no`, and nested lists and maps as compact
JSON.

With `--json`, list, `health` and `validate` commands print the raw API response instead. This is the
output to use in scripts. `--json` doesn't change audio commands (the audio is the output), and `remove` /
`delete` commands still print their confirmation line (the API returns `204 No Content`).

```bash
opal talk voices list                     # table
opal talk --json voices list              # JSON array, suitable for jq
opal talk --json engines validate kokoro  # the full ValidationResult object
```

### Audio output

`generate` and `voices test` always need `-o/--output`:

- `-o FILE` writes the audio to `FILE`.
- `-o -` writes the audio to **stdout**, so you can pipe it into a player or converter.

The summary line always goes to stderr, so it never corrupts piped audio:

```
wrote 55852 bytes to hello.wav (wav, 16000 Hz, s16le, voice=en-us-amy-low, engine=piper, profile=1)
```

It shows the format, sample rate, sample format, voice, engine, and the profile (when a profile was
used). These values come from the `X-Opal-*` response headers.

### Options

- **Engine options.** `-O/--option key=value` (repeatable) and `--options-json '{...}'` pass opaque,
  engine-specific `engine_options`. Each `-O` value is parsed as JSON when possible: `0.5` becomes a
  number, `true`/`false` a boolean, `null` a null, and `'"~/dir"'` a string. Anything that isn't valid
  JSON is kept as a plain string, so `-O lang=fr-fr` works without extra quotes. When both are given,
  `--options-json` is applied first, then each `-O` on top of it. See
  [12. Engine options reference](#12-engine-options-reference) for the keys each engine accepts.
- **Boolean flag pairs.** `--enabled/--disabled` (voices), `--enable/--disable` (engines) and
  `--active/--inactive` (profile `update`). In `update` commands, leaving out both flags keeps the
  current value.
- **Clearing optional fields.** `update` commands have explicit flags to clear a nullable field:
  `--clear-description`, `--clear-language` (voices) and `--native-rate` (profiles: clears
  `sample_rate`).
- **`update` needs at least one change.** Running `update` without any option fails with
  `Invalid value: nothing to update` and exit code `2`.
- **Client-side ranges.** `--speed` (`0.25`–`4.0`), `--volume` (`0.0`–`2.0`), `--sample-rate`
  (`8000`–`192000`) and `--profile` (`≥ 1`) are checked before any request is sent. An out-of-range
  value exits with code `2`.

### How `update` treats maps

| Command | `engine_options` / `options` on update |
|---|---|
| `engines update` | **Merged** (JSON merge-patch): supplied keys replace, `null` removes a key, other keys are kept |
| `voices update` | **Replaced**: the new map is the complete set |
| `profiles update` | **Replaced**: the new map is the complete set |

```bash
opal talk engines update piper -O cache_size=8         # other Piper options are kept
opal talk voices update my-voice -O noise_scale=0.4    # the voice now has ONLY noise_scale
opal talk voices update my-voice --options-json '{}'   # clear all voice engine_options
```

### Quoting

Quote multi-word arguments and text:

```bash
opal talk generate "Hello there. How are you today?" -o hello.wav
opal talk voices add lessac --name "Lessac (medium)" --engine piper --ref en_US-lessac-medium
opal talk engines update piper -O voices_dir='"~/piper/voices"'   # JSON string, '~' kept literally
```

### No confirmation prompts

No command asks for confirmation. `voices remove`, `profiles remove`, `settings delete`,
`backup delete`, `backup restore` and `backup import` take effect immediately, and `restore`/`import` do
**not** take an automatic safety backup. Run `opal talk backup create` first if you're unsure.

### Errors and exit codes

Service errors are printed as `Error: HTTP <status> <code>: <message>`. Request validation errors list
each failing field as `<location>: <message>`:

```
Error: HTTP 409 voice_in_use: voice 'en-us-amy-low' is referenced by 2 profile(s)
Error: HTTP 404 profile_not_found: profile 99 not found
Error: HTTP 422 validation error: body.engine_options.lang: unsupported option 'lang' (allowed: [...])
Error: HTTP 413 payload_too_large: text length 34 exceeds the configured maximum of 10 characters
```

| Exit code | Meaning |
|---|---|
| `0` | Success, including `validate` results that are `valid` with warnings |
| `1` | API error (4xx/5xx), `validate` result `INVALID`, `backup import` with `success: false`, or a stream that terminated prematurely |
| `2` | Invalid CLI usage (unknown option, missing argument, out-of-range value, `update` with nothing to change, `--format` with `--stream`), **or the service could not be reached** |

---

## 4. Command tree

```
opal (or opal-talk, which starts at 'talk')
└── talk [--api-url] [--json] [--timeout]
    ├── engines   list | get | update | validate
    ├── voices    list | get | add | update | remove | validate | test
    ├── profiles  list | get | add | update | remove | activate
    ├── generate  TEXT -o OUT [--stream] ...
    ├── settings  list | get | set | delete
    ├── backup    list | create | download | restore | delete | export | import | validate
    ├── health
    ├── info
    └── ping
```

---

## 5. `engines`: engine discovery and configuration

An engine is a TTS runtime that is installed on the host, such as Piper or Kokoro. Engines are
**discovered** by the service (built-in adapters plus plugins registered under the `opal_talk.engines`
entry point). You can't create or delete them through the CLI. You can only enable or disable them and
set their engine-scope options.

| Command | Description |
|---|---|
| `list` | List installed engines |
| `get ENGINE_ID` | Show one engine as JSON, including options and runtime metadata |
| `update ENGINE_ID [--enable/--disable] [-O key=value]... [--options-json JSON]` | Enable/disable routing, or merge engine options |
| `validate ENGINE_ID` | Check the runtime and configuration. Exits `1` if invalid. |

| Option | Meaning |
|---|---|
| `--enable/--disable` | Allow or block generation through this engine. Leaving both out keeps the current value. |
| `-O`, `--option key=value` | Engine option to set (repeatable). `key=null` removes the key. |
| `--options-json JSON` | Engine options as one JSON object (merged the same way) |

`enabled` is your administrative switch. `available` is computed by the service: the runtime package is
importable, and the resources it needs (for example, Kokoro's model and voices files) exist.

### Examples

```bash
# List engines (columns: engine_id, name, enabled, available)
opal talk engines list
```
```
ENGINE_ID  NAME    ENABLED  AVAILABLE
kokoro     Kokoro  yes      yes
piper      Piper   yes      yes
```

```bash
# Show one engine: options and metadata (runtime version, native rate, speed range)
opal talk engines get kokoro
```
```json
{
  "engine_id": "kokoro",
  "name": "Kokoro",
  "description": "Kokoro-82M neural text-to-speech via kokoro-onnx",
  "enabled": true,
  "available": true,
  "options": {
    "model_path": "~/kokoro/kokoro-v1.0.int8.onnx",
    "voices_path": "~/kokoro/voices-v1.0.bin",
    "default_lang": "en-us"
  },
  "metadata": {
    "adapter": "opal_talk.tts.kokoro_adapter",
    "runtime": "kokoro-onnx",
    "native_sample_rate": 24000,
    "speed_range": [0.5, 2.0],
    "version": "0.6.1"
  }
}
```

```bash
# Point Piper at a voices directory, so voices can be registered by name
opal talk engines update piper -O voices_dir='"~/piper/voices"'

# Keep more Piper voices loaded at once, then remove the key again (back to the default of 4)
opal talk engines update piper -O cache_size=8
opal talk engines update piper -O cache_size=null

# Use the full-precision Kokoro model
opal talk engines update kokoro -O model_path='~/kokoro/kokoro-v1.0.onnx'

# Several options at once
opal talk engines update kokoro --options-json '{"model_path": "~/kokoro/kokoro-v1.0.onnx", "default_lang": "en-gb"}'

# Take an engine out of service, and put it back
opal talk engines update kokoro --disable
opal talk engines update kokoro --enable

# Validate an engine
opal talk engines validate piper           # valid
opal talk engines validate kokoro
```

Example `validate` outputs:

```
valid
  warning: engine_disabled: engine 'kokoro' is administratively disabled
```
```
INVALID
  error: engine_unavailable: Kokoro model file '/nope.onnx' not found
```

> Invalid option values are rejected with HTTP `422` and nothing is stored, for example
> `body.options.cache_size: option 'cache_size' must be between 1 and 32`.

> `opal talk engines validate kokoro` also loads the Kokoro model and counts the voices in the voices
> file, so it catches unreadable files, not just missing ones.

---

## 6. `voices`: voice registrations

A voice is a **registration** that points at a voice or model resource that is already installed on the
host. It gives that resource a stable OPAL `voice_id` and binds it to an engine. The CLI never uploads,
downloads or deletes model files.

`engine_voice_ref` (`--ref`) is opaque to everything except the engine adapter:

| Engine | `--ref` is | Resolution |
|---|---|---|
| `piper` | A model name such as `en_US-amy-low`, or a path to a `.onnx` file | A ref that is absolute or contains `/` is used as a path. Otherwise the service tries `<voices_dir>/<ref>` and `<voices_dir>/<ref>.onnx`. The model config is `<model>.json`, unless the voice sets `config_path`. |
| `kokoro` | A Kokoro voice name such as `af_heart` or `bm_george` | Must exist in the configured `voices_path` file. The first letter selects the language (`a` = en-us, `b` = en-gb, ...). |

| Command | Description |
|---|---|
| `list [--engine E] [--enabled/--disabled]` | List voices, optionally filtered |
| `get VOICE_ID` | Show one voice as JSON, including resolved metadata |
| `add VOICE_ID --name N --engine E --ref R [OPTIONS]` | Register a voice |
| `update VOICE_ID [OPTIONS]` | Change a registration (`engine_options` are **replaced**) |
| `remove VOICE_ID` | Unregister a voice. Host files are never touched. |
| `validate VOICE_ID` | Check that the registration resolves and is usable. Exits `1` if invalid. |
| `test VOICE_ID -o OUT [OPTIONS]` | Generate a short test utterance with exactly this voice |

### `add` and `update` options

| Option | `add` | `update` | Meaning |
|---|---|---|---|
| `VOICE_ID` | required | required | Unique id (see [Conventions](#referencing-resources)) |
| `--name N` | required | optional | Human-readable name (1–200 chars) |
| `--engine E` | required | optional | Engine that interprets the reference |
| `--ref R` | required | optional | Opaque engine voice reference |
| `--description D` | optional | optional | Free text (≤1000 chars) |
| `--clear-description` | | optional | Set `description` to null |
| `--language L` | optional | optional | Language tag, e.g. `en-US`. On `add`, it's detected from the model when omitted. |
| `--clear-language` | | optional | Set `language` to null |
| `--enabled/--disabled` | default `--enabled` | optional | Whether the voice can be used |
| `-O key=value`, `--options-json` | optional | optional | Voice `engine_options` (voice-scope and generation-scope keys) |

When the engine is available, `add` and `update` resolve the reference right away. They fill in
`native_sample_rate`, `metadata` (model and config paths, quality, speakers, ...) and, if you didn't give
one, `language`. A reference that can't be resolved is rejected with `422`. If the engine is unavailable
at that moment, the voice is stored anyway with `native_sample_rate: null`.

### Examples: registering and managing voices

```bash
# List voices (columns: voice_id, name, engine_id, engine_voice_ref, language, enabled, native_sample_rate)
opal talk voices list
```
```
VOICE_ID             NAME                     ENGINE_ID  ENGINE_VOICE_REF     LANGUAGE  ENABLED  NATIVE_SAMPLE_RATE
en-us-amy-low        Amy (low)                piper      en_US-amy-low        en-US     yes      16000
en-us-lessac-medium  Lessac (medium)          piper      en_US-lessac-medium  en-US     yes      22050
kokoro-af-heart      Kokoro Heart             kokoro     af_heart             en-US     yes      24000
kokoro-bm-george     Kokoro George (British)  kokoro     bm_george            en-GB     yes      24000
```

```bash
# Filter by engine, or by enabled state
opal talk voices list --engine kokoro
opal talk voices list --disabled

# Show one voice with its resolved metadata
opal talk voices get en-us-amy-low

# Piper voice by name (resolved against the engine's voices_dir)
opal talk voices add amy-medium --name "Amy (medium)" --engine piper --ref en_US-amy-medium

# Piper voice by absolute path, with a non-default config file and a default noise level
opal talk voices add custom --name "Custom voice" --engine piper \
  --ref /opt/voices/custom.onnx -O config_path=/opt/voices/custom.json -O noise_scale=0.5

# Multi-speaker Piper model with a default speaker (see 'voices get' -> metadata.speakers)
opal talk voices add libritts-3 --name "LibriTTS speaker 3" --engine piper \
  --ref en_US-libritts_r-medium -O speaker_id=3

# Kokoro voices (the language is derived from the name prefix)
opal talk voices add kokoro-am-michael --name "Kokoro Michael" --engine kokoro --ref am_michael
opal talk voices add kokoro-ff-siwis --name "Kokoro Siwis (French)" --engine kokoro --ref ff_siwis

# Register a voice but keep it disabled for now
opal talk voices add staging-voice --name Staging --engine piper --ref en_US-bobby-medium --disabled

# Rename, describe, enable
opal talk voices update staging-voice --name "Bobby" --description "Casual male voice" --enabled

# Replace the voice's engine_options (the new map is the complete set)
opal talk voices update amy-medium -O noise_scale=0.4 -O noise_w_scale=0.6

# Clear optional fields
opal talk voices update amy-medium --clear-description --clear-language

# Unregister a voice (the .onnx files stay where they are)
opal talk voices remove staging-voice
```

`update` and `remove` protect profiles that use the voice:

| Situation | Result |
|---|---|
| `update --disabled` on the voice of the **active** profile | `409 voice_in_use` |
| `update --engine E` when a profile that uses the voice has `engine_options` that `E` doesn't accept | `409 profile_options_incompatible` |
| `update --engine E` with an engine that isn't installed | `404 engine_not_found` |
| `remove` while any profile references the voice | `409 voice_in_use` (the error lists the profile ids) |

### `voices validate`

```bash
opal talk voices validate kokoro-bm-george     # valid
opal talk voices validate staging-voice
```
```
valid
  warning: voice_disabled [enabled]: voice 'staging-voice' is disabled
```

| Code | Level | Meaning |
|---|---|---|
| `engine_not_found` | error | The voice's engine isn't installed |
| `engine_unavailable` | error | The runtime or its resources are missing |
| `model_not_found` | error | Piper: the model file can't be found |
| `model_config_not_found` / `model_config_invalid` | error | Piper: the `.onnx.json` config is missing, unreadable, or has no `audio.sample_rate` |
| `voice_not_found` | error | Kokoro: the voice name isn't in the voices file |
| `unsupported_option` / `invalid_option` | error | A stored `engine_options` key is unknown or has a bad value |
| `voice_disabled` | warning | The voice is disabled |
| `engine_disabled` | warning | The voice's engine is disabled |

### `voices test`

```
opal talk voices test VOICE_ID -o OUT [--text T] [--speed F] [--volume F] [--format wav|pcm] [--sample-rate HZ] [-O key=value]...
```

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--output OUT` | required | Output file, or `-` for stdout |
| `--text T` | `This is a test of the selected OPAL Talk voice.` | Text to speak |
| `--speed F` | `1.0` | `0.25`–`4.0` (Kokoro supports `0.5`–`2.0`) |
| `--volume F` | `1.0` | Linear gain `0.0`–`2.0` |
| `--format F` | `wav` | `wav` or `pcm` |
| `--sample-rate HZ` | voice's native rate | Output rate, `8000`–`192000` |
| `-O`, `--options-json` | | Generation `engine_options` for this test |

`voices test` uses exactly the given voice and engine. It ignores profiles and the `default_*` settings,
so it's the most direct way to hear a registration. A disabled voice or engine gives `409`.

```bash
# Default test phrase as WAV
opal talk voices test kokoro-af-heart -o test.wav
# wrote 145452 bytes to test.wav (wav, 24000 Hz, s16le, voice=kokoro-af-heart, engine=kokoro)

# Custom phrase, faster, as raw PCM
opal talk voices test en-us-amy-low -o test.pcm --format pcm --text "Testing one two" --speed 1.3

# Listen straight away
opal talk voices test kokoro-bm-george -o - | aplay
```

---

## 7. `profiles`: generation profiles

A profile is a reusable set of generation settings: a voice plus speed, volume, output format,
sample rate and generation `engine_options`. **At most one profile is active.** `generate` uses the
active profile unless you pass `--profile ID` or override individual values.

The seeded configuration ([config/profiles.yaml](../config/profiles.yaml)) creates:

| Id | Name | Voice | Notes |
|---|---|---|---|
| `1` | `default` | `en-us-amy-low` | Active. WAV at the voice's native rate. |
| `2` | `kokoro` | `kokoro-af-heart` | Speed `1.1` |
| `3` | `telephony` | `en-us-amy-low` | Raw PCM at 8 kHz, `noise_scale: 0.5` |

| Command | Description |
|---|---|
| `list` | List profiles |
| `get ID` | Show one profile as JSON |
| `add NAME --voice V [OPTIONS]` | Create a profile |
| `update ID [OPTIONS]` | Partially update a profile (`engine_options` are **replaced**) |
| `activate ID` | Make this profile the active one (all others are deactivated) |
| `remove ID` | Delete a profile. The active profile can't be deleted. |

### Profile options

| Option | `add` default | Meaning |
|---|---|---|
| `NAME` (positional, `add` only) / `--name N` (`update`) | | Unique name, 1–100 chars |
| `--voice V` | required on `add` | Registered `voice_id` |
| `--description D` | none | Free text. `--clear-description` (update) sets it to null. |
| `--speed F` | `1.0` | `0.25`–`4.0` |
| `--volume F` | `1.0` | Linear gain `0.0`–`2.0` |
| `--format F` | `wav` | `wav` or `pcm` |
| `--sample-rate HZ` | none (native rate) | `8000`–`192000`. `--native-rate` (update) clears it. |
| `--active` (add) / `--active/--inactive` (update) | not active | Activate on creation, or change the active flag |
| `-O key=value`, `--options-json JSON` | `{}` | Generation `engine_options` for the voice's engine |

Rules the service enforces:

| Situation | Result |
|---|---|
| Duplicate profile name | `409 profile_name_exists` |
| Unknown `--voice` | `404 voice_not_found` |
| `engine_options` that the voice's engine doesn't accept at generation scope | `422` |
| Activating (via `activate`, `add --active` or `update --active`) a profile whose voice is disabled, or whose engine is missing, disabled or unavailable | `409 voice_disabled`, `engine_not_found`, `engine_disabled` or `engine_unavailable` |
| `remove` on the active profile | `409 profile_active` |

> **Speed is checked at generation time, not on the profile.** You can save `--speed 3` on a Kokoro
> profile, but generating with it fails with `409 unsupported_speed` because Kokoro supports only
> `0.5`–`2.0`.

### Examples

```bash
# List profiles (columns: id, name, is_active, voice_id, speed, volume, output_format, sample_rate)
opal talk profiles list
```
```
ID  NAME       IS_ACTIVE  VOICE_ID         SPEED  VOLUME  OUTPUT_FORMAT  SAMPLE_RATE
1   default    yes        en-us-amy-low    1.0    1.0     wav            -
2   kokoro     no         kokoro-af-heart  1.1    1.0     wav            -
3   telephony  no         en-us-amy-low    1.0    1.0     pcm            8000
```

```bash
# Show one profile
opal talk profiles get 3

# Create a profile
opal talk profiles add narrator --voice kokoro-bm-george --speed 0.9 --description "Audiobook narration"

# Create and activate in one step
opal talk profiles add main --voice en-us-lessac-medium --active

# Telephony-style profile: raw PCM, 8 kHz, calmer Piper delivery
opal talk profiles add phone --voice en-us-amy-low --format pcm --sample-rate 8000 -O noise_scale=0.5

# Change some fields (the others are unchanged)
opal talk profiles update 4 --volume 1.2 --sample-rate 16000

# Back to the voice's native rate, and drop the description
opal talk profiles update 4 --native-rate --clear-description

# Replace the profile's engine_options (Kokoro: force British pronunciation)
opal talk profiles update 4 -O lang=en-gb

# Clear the profile's engine_options
opal talk profiles update 4 --options-json '{}'

# Switch the active profile
opal talk profiles activate 4
opal talk profiles activate 1

# Delete a profile (activate another one first if it's active)
opal talk profiles remove 4
```

> `profiles update ID --inactive` on the active profile leaves the service with **no** active profile.
> `generate` then needs `--voice` or `--profile`, otherwise it fails with `409 no_voice`.

---

## 8. `generate`: speech generation and streaming

```
opal talk generate TEXT -o OUT [--stream] [--profile ID] [--voice V] [--engine E]
                   [--speed F] [--volume F] [--format wav|pcm] [--sample-rate HZ]
                   [-O key=value]... [--options-json JSON]
```

| Option | Default | Meaning |
|---|---|---|
| `TEXT` | required | Plain text to speak. `-` reads the text from stdin. |
| `-o`, `--output OUT` | required | Output file, or `-` for stdout |
| `--stream` | off | Use `/generate/stream`: progressive raw PCM, written as it arrives |
| `--profile ID` | active profile | Use this profile instead of the active one |
| `--voice V` | from profile | Voice override |
| `--engine E` | voice's engine | Advanced engine override. The engine must be able to use the voice. |
| `--speed F` | see below | `0.25`–`4.0` (Kokoro: `0.5`–`2.0`) |
| `--volume F` | see below | Linear gain `0.0`–`2.0`. Samples are clipped to full scale. |
| `--format F` | see below | `wav` or `pcm`. Not allowed with `--stream`, which is always PCM. |
| `--sample-rate HZ` | see below | Output rate `8000`–`192000`. Audio is resampled when it differs from the native rate. |
| `-O`, `--options-json` | | Request-level generation `engine_options` |

### How values are resolved

Each value comes from the first source that sets it:

| Value | 1. Request | 2. Profile | 3. Setting | 4. Built-in |
|---|---|---|---|---|
| Voice | `--voice` | profile `voice_id` | | (`409 no_voice`) |
| Engine | `--engine` | | | voice's `engine_id` |
| Speed | `--speed` | profile `speed` | `default_speed` | `1.0` |
| Volume | `--volume` | profile `volume` | `default_volume` | `1.0` |
| Format | `--format` | profile `output_format` | `default_output_format` | `wav` |
| Sample rate | `--sample-rate` | profile `sample_rate` | | voice's native rate |

"Profile" means the `--profile ID` profile, or the active profile if you don't pass `--profile`.

`engine_options` are layered, and later layers win key by key: **voice → profile → request**. Profile
`engine_options` are only applied when the request is routed to the same engine as the profile's own
voice. If you override `--voice` with a voice on a different engine, the profile's options are skipped,
because they belong to the other engine.

### Output

| Mode | Body | Format |
|---|---|---|
| `wav` | Complete WAV file | RIFF/WAVE, signed 16-bit PCM, mono |
| `pcm` | Raw samples, no header | Signed 16-bit little-endian (`s16le`), mono |
| `--stream` | Raw samples, written as they're generated | Signed 16-bit little-endian (`s16le`), mono |

Raw PCM has no header, so you need the sample rate to play it. The summary line on stderr shows it, and
`opal talk voices list` shows each voice's native rate. To avoid guessing, pass `--sample-rate`.

Long text is split internally into chunks at sentence boundaries (at most `chunk_max_chars` characters
each; see [settings](#9-settings-service-settings)). The chunks are joined seamlessly and the
boundaries are not visible in the output. Text longer than `max_text_chars` is rejected with `413`.

### Examples

```bash
# Use the active profile
opal talk generate "Hello from OPAL Talk." -o hello.wav
# wrote 55852 bytes to hello.wav (wav, 16000 Hz, s16le, voice=en-us-amy-low, engine=piper, profile=1)

# Use a specific profile
opal talk generate "Hello" --profile 2 -o kokoro.wav

# The telephony profile produces raw 8 kHz PCM
opal talk generate "Your call is important to us." --profile 3 -o prompt.raw

# Pick a voice, speed it up, make it quieter, and force 16 kHz
opal talk generate "Hello" --voice kokoro-bm-george --speed 1.2 --volume 0.8 --sample-rate 16000 -o george.wav

# Profile, but override its format
opal talk generate "Hello" --profile 2 --format pcm -o hello.pcm

# Engine-specific options (Kokoro language, Piper noise)
opal talk generate "Bonjour tout le monde" --voice kokoro-ff-siwis -O lang=fr-fr -o fr.wav
opal talk generate "Hello" --voice en-us-amy-low -O noise_scale=0.3 -O noise_w_scale=0.5 -o calm.wav

# Read the text from a file or a pipe
opal talk generate - -o chapter1.wav < chapter1.txt
echo "Piped text works too." | opal talk generate - -o piped.wav

# Write to stdout and play it right away
opal talk generate "Dinner is ready." -o - | aplay

# Advanced: engine override (only works if that engine can resolve the voice)
opal talk generate "Hello" --voice kokoro-af-heart --engine kokoro -o x.wav
```

### Streaming

With `--stream`, the CLI calls `/api/v1/generate/stream` and writes raw S16LE mono PCM **as soon as each
block arrives**, so playback can start before the whole text has been synthesized:

```bash
# Play while it's being generated. Match -r to the output rate.
opal talk generate "Streaming is progressive. The first sentence plays while the rest is synthesized." \
  --stream -o - | aplay -r 16000 -f S16_LE -c 1 -t raw

# Force a known rate so the player settings never change, whatever the voice
opal talk generate "$(cat story.txt)" --stream --sample-rate 22050 -o - | aplay -r 22050 -f S16_LE -c 1 -t raw

# PulseAudio / PipeWire
opal talk generate "Hello" --stream --sample-rate 24000 -o - | pacat --raw --rate=24000 --format=s16le --channels=1

# ffplay
opal talk generate "Hello" --stream --sample-rate 24000 -o - | ffplay -autoexit -nodisp -f s16le -ar 24000 -ac 1 -i -

# Save the stream, then wrap it as WAV
opal talk generate "Saved stream" --stream -o s.pcm
sox -t raw -r 16000 -e signed -b 16 -c 1 s.pcm s.wav
ffmpeg -f s16le -ar 16000 -ac 1 -i s.pcm s.wav
```

| Behavior | Details |
|---|---|
| Format | Always raw PCM. `--format` with `--stream` exits with code `2`. |
| Sample rate | Same resolution as above. The CLI prints it in the summary line once the stream ends. |
| Early errors | Problems that occur before the first audio block (unknown voice, bad options, text too long, ...) are normal JSON errors, exactly like `generate` without `--stream`. |
| Late errors | If the engine fails after audio has started, the service aborts the response. The CLI prints `Error: stream terminated prematurely; generation failed (...)` and exits `1`. Audio that was already received has been written to the output. |

### Generation errors

| Status | Code | Cause |
|---|---|---|
| `409` | `profile_not_found` | `--profile ID` doesn't exist |
| `409` | `voice_not_found` | `--voice` (or the profile's voice) isn't registered |
| `409` | `no_voice` | No `--voice`, no `--profile`, and no active profile |
| `409` | `voice_disabled` | The resolved voice is disabled |
| `409` | `engine_not_found` / `engine_disabled` | The engine isn't installed, or is disabled |
| `409` | `voice_engine_mismatch` | `--engine` can't use the resolved voice |
| `409` | `unsupported_speed` | Speed is outside the engine's range (Kokoro: `0.5`–`2.0`) |
| `413` | `payload_too_large` | Text is longer than `max_text_chars` |
| `422` | validation error | Empty text (`text contains nothing to speak`), bad `--format`, or unsupported/invalid `engine_options` |
| `503` | `engine_unavailable` | The engine runtime or its resources are missing |
| `500` | `generation_failed` | The engine failed while synthesizing |

---

## 9. `settings`: service settings

Settings are service-wide key/value pairs, stored as strings. Known keys are validated when you set
them. Unknown keys are accepted and stored as they are.

| Command | Description |
|---|---|
| `list` | Show all **stored** settings as a JSON map |
| `get KEY` | Show one stored setting (`404` if it isn't stored) |
| `set KEY VALUE` | Create or replace a setting |
| `delete KEY` | Delete a stored setting so the built-in default applies again |

### Known keys

| Key | Type | Built-in default | Allowed / range | Effect |
|---|---|---|---|---|
| `max_text_chars` | int | `20000` | 1 to 1000000 | Longer text is rejected with `413` |
| `chunk_max_chars` | int | `400` | 50 to 5000 | Maximum size of an internal synthesis chunk. Smaller chunks make streaming start sooner. |
| `default_speed` | float | `1.0` | 0.25 to 4.0 | Used when neither the request nor the profile sets a speed |
| `default_volume` | float | `1.0` | 0.0 to 2.0 | Used when neither the request nor the profile sets a volume |
| `default_output_format` | str | `wav` | `wav`, `pcm` | Used when neither the request nor the profile sets a format |

> `list` and `get` show only **stored** settings. A known key that isn't stored still has its built-in
> default, but `get` returns `404` for it. The seeded configuration stores `max_text_chars` and
> `chunk_max_chars`.

### Examples

```bash
# Show stored settings
opal talk settings list
```
```json
{
  "chunk_max_chars": "400",
  "max_text_chars": "20000"
}
```

```bash
# Show one setting
opal talk settings get chunk_max_chars

# Allow longer texts
opal talk settings set max_text_chars 100000

# Smaller chunks: streaming starts sooner
opal talk settings set chunk_max_chars 200

# Service-wide fallbacks (when the profile/request doesn't set them)
opal talk settings set default_speed 1.1
opal talk settings set default_output_format pcm

# Invalid values are rejected
opal talk settings set chunk_max_chars 10
# Error: HTTP 422 validation error: body.value: setting 'chunk_max_chars' must be between 50 and 5000

# Unknown keys are stored as they are (useful for your own tooling)
opal talk settings set deployment_note "kitchen pi"

# Go back to the built-in default
opal talk settings delete default_speed
```

---

## 10. `backup`: backup, restore, import, export and validation

| Command | Description |
|---|---|
| `validate` | Validate the current configuration. Exits `1` if invalid. |
| `create` | Create a backup on the service host |
| `list` | List backups, newest first (filename, created_at, size_bytes) |
| `download FILE [-o PATH]` | Download a backup (default `./FILE`, `-` for stdout) |
| `restore FILE` | Replace the configuration with a backup |
| `delete FILE` | Delete a backup on the service host |
| `export [-o FILE]` | Export the configuration as JSON (stdout, or a file) |
| `import` | Replace the configuration from the service's YAML config directory. Exits `1` if invalid. |

The configuration is made of four sections: **engines** (enabled flag and options), **voices**,
**profiles** and **settings**. Backups, exports and imports cover exactly these sections. They never
contain engine binaries or voice/model files.

### Validation

```bash
opal talk backup validate
```

The output is `valid` or `INVALID`, followed by any errors and warnings. `validate` checks:

- engine options of every installed engine
- every voice: duplicates, unknown engine, invalid `engine_options`, and whether the reference resolves
  on this host (unresolvable references are **errors** here)
- every profile: duplicate names, unknown voice, more than one active profile (error), no active profile
  (warning), active profile with a disabled voice (warning), invalid `engine_options`
- every stored setting against the known-key rules

### Backups

Backups are `.tar.gz` archives that contain a single JSON document, `opal-talk-config.json`, in the same
shape as `export`. They're stored in `<data_dir>/backups/` on the service host (default
`./var/backups/`) and named `opal-talk-<UTC timestamp>.tar.gz`, for example
`opal-talk-2026-10-12T162205Z.tar.gz`. A `-N` suffix is added if two backups are created in the same
second. Only files that follow this naming pattern are listed or accepted.

```bash
# Create a backup
opal talk backup create
```
```json
{
  "filename": "opal-talk-2026-10-12T162205Z.tar.gz",
  "message": "Backup created successfully."
}
```

```bash
# List backups, newest first
opal talk backup list
```
```
FILENAME                             CREATED_AT                   SIZE_BYTES
opal-talk-2026-10-12T162205Z.tar.gz  2026-10-12T16:22:05.719126Z  1208
```

```bash
# Download into the current directory, to a specific file, or to stdout
opal talk backup download opal-talk-2026-10-12T162205Z.tar.gz
opal talk backup download opal-talk-2026-10-12T162205Z.tar.gz -o ~/backups/before-upgrade.tar.gz
opal talk backup download opal-talk-2026-10-12T162205Z.tar.gz -o - | tar -xzOf - opal-talk-config.json | jq .voices

# Restore a backup (no prompt, takes effect immediately)
opal talk backup restore opal-talk-2026-10-12T162205Z.tar.gz
```
```json
{
  "success": true,
  "message": "Backup restored successfully.",
  "details": {
    "filename": "opal-talk-2026-10-12T162205Z.tar.gz",
    "warnings": [],
    "engines": 2,
    "voices": 4,
    "profiles": 4,
    "settings": 2
  }
}
```

```bash
# Delete an old backup
opal talk backup delete opal-talk-2026-10-01T080000Z.tar.gz
```

How `restore` works:

1. The archive is read and its JSON is checked against the export schema. An unreadable file gives
   `409 invalid_backup`.
2. The whole configuration is validated. Voice references that don't resolve on this host are only
   **warnings**, so a backup from another machine can still be restored. Real errors give
   `409 backup_invalid` with the list of errors, and nothing is changed.
3. Engines, voices, profiles and settings are replaced in one transaction. Profile ids are kept.

> `restore` does **not** create a safety backup first. Run `opal talk backup create` before you restore
> if you might want to go back.

### Export

```bash
# Export to stdout
opal talk backup export > talk-config.json

# Export to a file
opal talk backup export -o talk-config.json
```

The export contains `version`, `exported_at`, `engines`, `voices`, `profiles` and `settings`. It's
useful for inspection, diffs and auditing.

> The CLI and the API can't import an export JSON directly. To apply a configuration, either restore a
> backup (which contains the same JSON), or edit the YAML files in the service's config directory and
> run `backup import`.

### Import

`import` reads the YAML files in the **service's** config directory (`config_dir` in `service.yaml`,
default `./config`). There is no `--from-dir` option: the files must be on the service host.

| File | Top-level key | Content |
|---|---|---|
| `engines.yaml` | `engines` | Map of `engine_id` → `{enabled, options}` |
| `voices.yaml` | `voices` | List of voices (same fields as `voices add`) |
| `profiles.yaml` | `profiles` | List of profiles (same fields as `profiles add`, plus `is_active`) |
| `settings.yaml` | `settings` | Map of key → value |

Only the files that are present replace their section. A missing file keeps that section unchanged.
Everything is validated before anything is applied.

```bash
opal talk backup import
```
```json
{
  "success": true,
  "validation": {
    "valid": true,
    "errors": [],
    "warnings": [],
    "details": {
      "engines": 2, "voices": 4, "profiles": 3, "settings": 2,
      "files": ["engines.yaml", "voices.yaml", "profiles.yaml", "settings.yaml"]
    }
  },
  "message": "Configuration imported successfully."
}
```

| Outcome | Result |
|---|---|
| Valid | `success: true`. The sections are replaced. Exit `0`. |
| Invalid YAML or configuration | `success: false` with the validation errors. **Nothing is applied.** Exit `1`. |
| No config files in the directory | `409 config_not_found`. Exit `1`. |
| Engine in `engines.yaml` that isn't installed | Warning `engine_not_installed`. Its entry is ignored. |
| Voice reference that doesn't resolve on this host | Warning only. The voice is imported. |

> `import` **replaces** the sections it reads. Voices or profiles that aren't in the files are deleted,
> and profiles get new ids `1..N` in file order. It does **not** create a safety backup first. Run
> `opal talk backup create` before importing.

The service also runs this import automatically on its first start, when the database is new.

---

## 11. `health`, `info`, `ping`: service status

| Command | Description |
|---|---|
| `ping` | Liveness probe (`GET /healthz`). Prints `ok`. |
| `info` | Service name, version and API version (`GET /`) |
| `health` | Readiness: overall status, active profile, and per-engine state (`GET /api/v1/health/`) |

```bash
opal talk ping
opal talk info
```
```json
{
  "name": "opal-talk-svc",
  "version": "1.0.0",
  "api_version": "v1",
  "description": "Implementation-neutral OPAL text-to-speech service"
}
```

```bash
opal talk health
```
```
status: degraded  version: 1.0.0  active_profile_id: 1
ENGINE_ID  ENABLED  AVAILABLE  MESSAGE
kokoro     yes      no         Kokoro model file '/nope.onnx' not found
piper      yes      yes        -
```

| `status` | Meaning |
|---|---|
| `ok` | Every enabled engine is available |
| `degraded` | At least one enabled engine is unavailable, but at least one works |
| `error` | No enabled engine is available. Generation can't succeed. |

`health` always exits `0` when the service answers. Use `--json` and check `.status` in scripts. The JSON
also has `details` with voice, enabled-voice and profile counts:

```bash
opal talk --json health | jq -r .status
```

---

## 12. Engine options reference

`engine_options` are opaque to the service core. Each adapter declares which keys it accepts, and at
which **scope**:

| Scope | Where it's set | CLI |
|---|---|---|
| `engine` | On the engine itself; applies service-wide | `engines update E -O ...` |
| `voice` | On a voice registration; voices also accept generation keys as per-voice defaults | `voices add/update -O ...` |
| `generation` | On a profile, a request, or a voice test | `profiles add/update -O ...`, `generate -O ...`, `voices test -O ...` |

A key used at the wrong scope, or one the engine doesn't know, is rejected with `422` and the list of
allowed keys. A value with the wrong type or range is rejected with `422`. Values must have the right
**JSON type**, so `-O cache_size=8` (number) works, but `-O cache_size='"8"'` (string) is rejected.

### Piper (`piper`)

| Key | Scope | Type | Default | Meaning |
|---|---|---|---|---|
| `voices_dir` | engine | string or null | `null` | Directory used to resolve voice refs given by name (`en_US-amy-low` → `<dir>/en_US-amy-low.onnx`). `~` is expanded. |
| `use_cuda` | engine | bool | `false` | Run ONNX inference on CUDA |
| `cache_size` | engine | int 1–32 | `4` | Number of loaded voice models kept in memory (LRU) |
| `config_path` | voice | string | `<model>.json` | Explicit path of the voice's `.onnx.json` config |
| `speaker_id` | generation | int 0–100000 | | Speaker index for multi-speaker models (must be below the model's speaker count) |
| `speaker` | generation | string | | Speaker name for multi-speaker models (from the model's `speaker_id_map`; overrides `speaker_id`) |
| `noise_scale` | generation | number 0–2 | model default | Phoneme noise (expressiveness) |
| `noise_w_scale` | generation | number 0–2 | model default | Phoneme-width noise (rhythm variation) |
| `normalize_audio` | generation | bool | `true` | Let Piper normalize the output level |

Piper supports the full portable speed range `0.25`–`4.0`. Speed is applied through Piper's length scale.

### Kokoro (`kokoro`)

| Key | Scope | Type | Default | Meaning |
|---|---|---|---|---|
| `model_path` | engine | string or null | `null` | Path of `kokoro-v1.0.onnx` or `kokoro-v1.0.int8.onnx`. Required. |
| `voices_path` | engine | string or null | `null` | Path of `voices-v1.0.bin`. Required. |
| `default_lang` | engine | language code | `en-us` | Fallback language when the voice name's prefix isn't recognized |
| `lang` | generation | language code | from voice prefix | Pronunciation language for this voice, profile or request |

Language codes: `en-us`, `en-gb`, `es`, `fr-fr`, `hi`, `it`, `ja`, `pt-br`, `cmn`. The voice prefix maps
to them as `a`→`en-us`, `b`→`en-gb`, `e`→`es`, `f`→`fr-fr`, `h`→`hi`, `i`→`it`, `j`→`ja`, `p`→`pt-br`,
`z`→`cmn`. The second letter of a Kokoro voice name is its gender (`f`/`m`).

Kokoro always generates audio at 24 kHz and supports speed `0.5`–`2.0` only. Other speeds give
`409 unsupported_speed` and are not silently clamped.

---

## 13. Walkthroughs

### 13.1 Full tour

This tour exercises every command group and cleans up after itself. Run it against a service that has
the seeded configuration.

```bash
# --- sanity -------------------------------------------------------------------------------
opal talk ping
opal talk info
opal talk health
opal talk backup create                                          # safety copy

# --- engines ------------------------------------------------------------------------------
opal talk engines list
opal talk engines get piper
opal talk engines validate piper
opal talk engines update piper -O cache_size=8
opal talk engines update piper -O cache_size=null                # back to the default

# --- voices -------------------------------------------------------------------------------
opal talk voices list
opal talk voices add tour-voice --name "Tour" --engine piper --ref en_US-amy-medium --disabled
opal talk voices validate tour-voice                             # valid, warning: voice_disabled
opal talk voices update tour-voice --enabled -O noise_scale=0.4
opal talk voices test tour-voice -o tour-test.wav

# --- profiles -----------------------------------------------------------------------------
opal talk profiles add tour --voice tour-voice --speed 1.1 --description "Tour profile"
TOUR=$(opal talk --json profiles list | jq '.[] | select(.name=="tour") | .id')
opal talk profiles update "$TOUR" --volume 0.9 --sample-rate 16000
opal talk profiles activate "$TOUR"
opal talk generate "This is the tour profile speaking." -o tour.wav
opal talk generate "And this one is streamed." --stream -o tour.pcm
opal talk profiles activate 1                                    # back to default before removing

# --- settings -----------------------------------------------------------------------------
opal talk settings list
opal talk settings set default_volume 0.9
opal talk settings get default_volume
opal talk settings delete default_volume

# --- configuration ------------------------------------------------------------------------
opal talk backup validate
opal talk backup export -o tour-export.json
opal talk backup list

# --- clean up -----------------------------------------------------------------------------
opal talk profiles remove "$TOUR"
opal talk voices remove tour-voice
rm -f tour-test.wav tour.wav tour.pcm tour-export.json
```

### 13.2 Add a new Piper voice

```bash
# 1. Install the model files on the service host (OPAL Talk never downloads them)
cd ~/piper/voices
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json

# 2. Make sure Piper resolves names against this directory
opal talk engines get piper | jq .options.voices_dir

# 3. Register, validate and listen
opal talk voices add en-gb-alan --name "Alan (British)" --engine piper --ref en_GB-alan-medium
opal talk voices validate en-gb-alan
opal talk voices test en-gb-alan -o - | aplay

# 4. Use it from a profile
opal talk profiles add british --voice en-gb-alan --active
opal talk generate "Good evening." -o evening.wav
```

### 13.3 Add a Kokoro voice

```bash
# 1. Point the engine at the model and voices files (once)
opal talk engines update kokoro -O model_path='~/kokoro/kokoro-v1.0.int8.onnx' -O voices_path='~/kokoro/voices-v1.0.bin'
opal talk engines validate kokoro

# 2. Register voices by Kokoro name
opal talk voices add kokoro-am-adam --name "Kokoro Adam" --engine kokoro --ref am_adam
opal talk voices add kokoro-bf-emma --name "Kokoro Emma" --engine kokoro --ref bf_emma

# 3. Compare them
for v in kokoro-am-adam kokoro-bf-emma; do
  opal talk voices test "$v" --text "The quick brown fox jumps over the lazy dog." -o "$v.wav"
done
```

### 13.4 Stream long text to the speakers

```bash
# Smaller chunks mean the first audio arrives sooner
opal talk settings set chunk_max_chars 200

# A fixed output rate keeps the player settings the same for every voice
opal talk generate - --stream --sample-rate 22050 -o - < article.txt \
  | aplay -r 22050 -f S16_LE -c 1 -t raw
```

### 13.5 Telephony prompts (8 kHz PCM)

```bash
# The seeded 'telephony' profile (id 3) produces raw 8 kHz PCM
opal talk profiles get 3

# Generate prompts in a loop
while IFS='|' read -r name text; do
  opal talk generate "$text" --profile 3 -o "prompts/$name.raw"
done < prompts.txt

# Or as 8 kHz WAV from the same profile
opal talk generate "Please hold." --profile 3 --format wav -o hold.wav
```

### 13.6 Back up and move the configuration to another machine

```bash
# On the source machine
opal talk backup create
opal talk backup list
opal talk backup download opal-talk-2026-10-12T162205Z.tar.gz -o ./talk-backup.tar.gz

# On the target machine: copy the file into the service's backup directory, then restore it
scp ./talk-backup.tar.gz new-pi:/home/pi/opal-talk/var/backups/opal-talk-2026-10-12T162205Z.tar.gz
export OPAL_TALK_API_URL=http://new-pi.local:8001
opal talk backup list
opal talk backup restore opal-talk-2026-10-12T162205Z.tar.gz
opal talk backup validate            # voice references must resolve on the new host
opal talk health
```

> The backup file must keep its `opal-talk-<timestamp>.tar.gz` name, otherwise the service doesn't see
> it. Voice/model files are not in the backup, so install them on the new host as well.

### 13.7 Keep the configuration in YAML and git

```bash
# On the service host: the config dir is the source of truth
cd /home/pi/opal-talk/config
git init && git add *.yaml && git commit -m "opal talk baseline"

# Edit, then apply (take a backup first: import has no automatic safety backup)
$EDITOR voices.yaml
opal talk backup create
opal talk backup import || echo "invalid: nothing was applied"
opal talk backup validate
```

---

## 14. Scripting and automation

### JSON output with `jq`

```bash
# All voice ids
opal talk --json voices list | jq -r '.[].voice_id'

# Enabled voices with their native rates
opal talk --json voices list --enabled | jq -r '.[] | "\(.voice_id)\t\(.native_sample_rate)"'

# The active profile's id and name
opal talk --json profiles list | jq -r '.[] | select(.is_active) | "\(.id) \(.name)"'

# A profile id by name
opal talk --json profiles list | jq '.[] | select(.name=="telephony") | .id'

# Unavailable engines and why
opal talk --json health | jq -r '.engines[] | select(.available | not) | "\(.engine_id): \(.message)"'

# Validation errors only
opal talk --json backup validate | jq -r '.errors[] | "\(.code) \(.field): \(.message)"'
```

### Shell assertions

```bash
#!/usr/bin/env bash
set -euo pipefail

opal-talk ping
[ "$(opal-talk --json health | jq -r .status)" = ok ] || { echo "service not healthy"; exit 1; }
opal-talk backup validate
for v in $(opal-talk --json voices list --enabled | jq -r '.[].voice_id'); do
  opal-talk voices validate "$v"
  opal-talk voices test "$v" --text "check" -o /dev/null
done
echo "all checks passed"
```

### CI: guard your configuration

```bash
# Fails the job (exit 1) if the YAML in the service's config dir is invalid,
# or if any voice no longer resolves on the host
opal-talk backup import
opal-talk backup validate
opal-talk generate "smoke test" -o /dev/null
```

### Bulk generation

```bash
# One WAV per line of a text file
n=0
while IFS= read -r line; do
  n=$((n+1))
  opal-talk generate "$line" -o "$(printf 'out/line-%03d.wav' "$n")"
done < lines.txt

# The same sentence with every enabled voice
opal-talk --json voices list --enabled | jq -r '.[].voice_id' \
  | xargs -I{} opal-talk generate "Hello from {}" --voice {} -o "{}.wav"
```

### Python without the CLI

The CLI is just an HTTP client, so anything it does can be done directly:

```python
import httpx

r = httpx.post("http://127.0.0.1:8001/api/v1/generate",
               json={"text": "Hello from Python", "voice_id": "kokoro-af-heart"}, timeout=120)
r.raise_for_status()
open("hello.wav", "wb").write(r.content)
print(r.headers["X-Opal-Sample-Rate"], r.headers["X-Opal-Engine-Id"])
```

See the [User Guide](USER_GUIDE.md#10-integrating-with-your-application) for streaming examples.

---

## 15. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Error: cannot reach OPAL Talk API: ...` (exit `2`) | The service isn't running, or the URL is wrong. Start `opal-talk-svc`, or set `OPAL_TALK_API_URL` / `--api-url`. The default port is `8001`, not `8000`. |
| `No such option: --json` | Global options must come right after `talk`: `opal talk --json voices list`. |
| `opal: No such command 'talk'` | Another package (OPAL Listen) owns the `opal` script. Use `opal-talk ...`, or reinstall this package. |
| `Invalid value for 'profile_id': 'narrator' is not a valid int` | Profiles are addressed by id. Look it up with `opal talk profiles list`. |
| `Invalid value: nothing to update` | `update` was called without any change option. |
| `422 ... engine_voice_ref: Piper voice model 'x' could not be resolved` | The model isn't in `voices_dir`, or the path is wrong. Check `opal talk engines get piper` and the files on the **service** host. |
| `422 ... unsupported option 'k' (allowed: [...])` | That key isn't valid for this engine at this scope. See [12. Engine options](#12-engine-options-reference). |
| `422 ... option 'k' must be of type int` | `-O` values are JSON. Write `-O cache_size=8`, not `-O cache_size='"8"'`. To pass a string that looks like JSON, quote it: `-O speaker='"3922"'`. |
| `409 unsupported_speed` | The engine doesn't support that speed (Kokoro: `0.5`–`2.0`). Check the request, the profile, and `default_speed`. |
| `409 no_voice` | No active profile. Activate one, or pass `--voice` / `--profile`. |
| `409 voice_in_use` | The voice is used by a profile (or by the active profile, when disabling). Change or remove those profiles first. |
| `409 profile_active` | Activate another profile before you remove this one. |
| `409 engine_disabled` | Run `opal talk engines update E --enable`. |
| `503 engine_unavailable` / health `degraded` | Install the runtime (`pip install -e '.[piper]'` or `'.[kokoro]'`), or fix the engine's paths. `opal talk health` shows the reason. |
| `413 payload_too_large` | Raise `max_text_chars`, or split the text. |
| Streamed audio plays too fast, too slow, or as noise | The player's rate doesn't match the output. Use the rate from the summary line, or force one with `--sample-rate`. Raw PCM is always S16LE mono. |
| `stream terminated prematurely` | The engine failed mid-stream. Check the service log. The output file contains only the audio received before the failure. |
| First `generate` with a voice is slow | The model is loaded on first use. Later calls reuse it (Piper keeps `cache_size` models). Raise `--timeout` for very long texts. |
| `backup restore` says `backup_not_found` for a copied file | The file must be in the service's `<data_dir>/backups/` and keep its `opal-talk-<timestamp>.tar.gz` name. |
| `backup import` says `config_not_found` | The service's `config_dir` has none of `engines.yaml`, `voices.yaml`, `profiles.yaml`, `settings.yaml`. |
| `/docs` loads but shows no operations, `/openapi.json` returns `500` | The service publishes `src/opal_talk/contract/opal_talk.yaml`, a symlink to `opal_talk.yaml` in the repository root. Make sure that file exists (the repo ships `opal_talk.1.0.0.yaml`). The CLI is not affected. |

---

## 16. Appendix: CLI ↔ REST mapping

All paths are relative to the API base URL. The operation ids are the `operationId`s in the OpenAPI
contract. The parity tests check that the CLI covers every operation.

| CLI (`opal talk ...`) | REST | operationId |
|---|---|---|
| `engines list` | `GET /api/v1/engines/` | `list_engines_api_v1_engines__get` |
| `engines get` | `GET /api/v1/engines/{engine_id}` | `get_engine_api_v1_engines__engine_id__get` |
| `engines update` | `PATCH /api/v1/engines/{engine_id}` | `update_engine_api_v1_engines__engine_id__patch` |
| `engines validate` | `POST /api/v1/engines/{engine_id}/validate` | `validate_engine_api_v1_engines__engine_id__validate_post` |
| `voices list` | `GET /api/v1/voices/` | `list_voices_api_v1_voices__get` |
| `voices add` | `POST /api/v1/voices/` | `create_voice_api_v1_voices__post` |
| `voices get` | `GET /api/v1/voices/{voice_id}` | `get_voice_api_v1_voices__voice_id__get` |
| `voices update` | `PATCH /api/v1/voices/{voice_id}` | `update_voice_api_v1_voices__voice_id__patch` |
| `voices remove` | `DELETE /api/v1/voices/{voice_id}` | `delete_voice_api_v1_voices__voice_id__delete` |
| `voices validate` | `POST /api/v1/voices/{voice_id}/validate` | `validate_voice_api_v1_voices__voice_id__validate_post` |
| `voices test` | `POST /api/v1/voices/{voice_id}/test` | `test_voice_api_v1_voices__voice_id__test_post` |
| `profiles list` | `GET /api/v1/profiles/` | `list_profiles_api_v1_profiles__get` |
| `profiles add` | `POST /api/v1/profiles/` | `create_profile_api_v1_profiles__post` |
| `profiles get` | `GET /api/v1/profiles/{profile_id}` | `get_profile_api_v1_profiles__profile_id__get` |
| `profiles update` | `PATCH /api/v1/profiles/{profile_id}` | `update_profile_api_v1_profiles__profile_id__patch` |
| `profiles remove` | `DELETE /api/v1/profiles/{profile_id}` | `delete_profile_api_v1_profiles__profile_id__delete` |
| `profiles activate` | `POST /api/v1/profiles/{profile_id}/activate` | `activate_profile_api_v1_profiles__profile_id__activate_post` |
| `generate` | `POST /api/v1/generate` | `generate_audio_api_v1_generate_post` |
| `generate --stream` | `POST /api/v1/generate/stream` | `stream_audio_api_v1_generate_stream_post` |
| `settings list` | `GET /api/v1/settings/` | `get_all_settings_api_v1_settings__get` |
| `settings get` | `GET /api/v1/settings/{key}` | `get_setting_api_v1_settings__key__get` |
| `settings set` | `PUT /api/v1/settings/{key}` | `upsert_setting_api_v1_settings__key__put` |
| `settings delete` | `DELETE /api/v1/settings/{key}` | `delete_setting_api_v1_settings__key__delete` |
| `health` | `GET /api/v1/health/` | `health_check_api_v1_health__get` |
| `backup list` | `GET /api/v1/backup/list` | `list_backups_api_v1_backup_list_get` |
| `backup create` | `POST /api/v1/backup/create` | `create_backup_api_v1_backup_create_post` |
| `backup download` | `GET /api/v1/backup/download/{filename}` | `download_backup_api_v1_backup_download__filename__get` |
| `backup restore` | `POST /api/v1/backup/restore/{filename}` | `restore_backup_api_v1_backup_restore__filename__post` |
| `backup delete` | `DELETE /api/v1/backup/{filename}` | `delete_backup_api_v1_backup__filename__delete` |
| `backup export` | `GET /api/v1/backup/export` | `export_config_api_v1_backup_export_get` |
| `backup import` | `POST /api/v1/backup/import` | `import_config_api_v1_backup_import_post` |
| `backup validate` | `GET /api/v1/backup/validate` | `validate_config_api_v1_backup_validate_get` |
| `info` | `GET /` | `root__get` |
| `ping` | `GET /healthz` | `healthz_healthz_get` |

See the [User Guide](USER_GUIDE.md) for the REST API, configuration files and architecture, and
[CONTRACT_NOTES.md](CONTRACT_NOTES.md) for how under-specified parts of the contract are interpreted.
