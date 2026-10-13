Implement the existing `opal_talk.yaml` OpenAPI contract as a complete, production-quality application.

## Source of truth

`opal_talk.yaml` is authoritative.

Do not redesign, add, remove, or rename REST operations, schemas, fields, or semantics unless required to correct an internally inconsistent contract. If a problem is found, document it rather than silently changing the API.

## Deliverables

Build:

1. **REST service**
   - Python
   - Service name: `opal-talk-svc`
   - OpenAPI contract-first implementation
   - Implement every operation in `opal_talk.yaml`
   - Support working Piper and Kokoro adapters
   - Keep the core TTS layer engine-neutral
   - WAV and PCM generation
   - Streaming PCM exactly as defined by the contract
   - Internal text chunking
   - No playback functionality

2. **CLI**
   - Python Typer
   - Root command:
     `opal talk`
   - Nested commands should mirror REST resources and operations, for example:
     `opal talk engines`
     `opal talk voices`
     `opal talk profiles`
     `opal talk generate`
   - REST API and CLI must remain in functional parity.
   - CLI communicates through the REST API rather than bypassing it.

3. **SPA**
   - React
   - Clean, minimal UI
   - Expose the same practical functionality as the REST API and CLI.
   - SPA communicates exclusively through the REST API.

## Architecture

Use a clear separation between:

- API layer
- application/service layer
- engine-neutral TTS abstractions
- Piper adapter
- Kokoro adapter
- persistence/configuration
- CLI client
- SPA

Engines must implement a common interface so additional TTS engines can be added without changing the public API.

Honor opaque `engine_options` without leaking engine-specific behavior into shared abstractions.

Voices register models/resources already installed on the host. Do not implement model uploads.

## Configuration

- YAML configuration
- Linux/bash-compatible
- `pyproject.toml`
- Sensible localhost defaults
- Persistent configuration/state where required by the contract

## Quality

Provide:

- full unit tests
- REST contract tests
- adapter tests
- CLI tests
- SPA tests for important workflows
- integration tests covering REST → service → engine adapter
- streaming tests
- validation/error-path tests

Use mocks only where appropriate; Piper and Kokoro adapters themselves must be functional.

## Contract parity

Create automated tests that verify:

- every OpenAPI operation has a server implementation
- CLI functionality remains aligned with REST functionality
- SPA functionality remains aligned with applicable REST functionality
- responses conform to `opal_talk.yaml`

## Delivery

Produce the complete repository, including source code, tests, configuration examples, installation instructions, development commands, and a concise README.

Favor simple, maintainable implementations over unnecessary abstraction.

Do not stop at scaffolding or pseudocode. Deliver a working implementation.