"""Engine-neutral generation: settings resolution, chunking, resampling, gain and encoding."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from opal_talk.persistence.store import Store
from opal_talk.schemas import GenerateRequest, GenerateStreamRequest, VoiceTestRequest
from opal_talk.service.catalog import EngineService
from opal_talk.service.errors import (
    ApiError,
    Conflict,
    GenerationFailed,
    InvalidRequest,
    NotFound,
    PayloadTooLarge,
    Unavailable,
)
from opal_talk.service.settings import SettingsService
from opal_talk.tts.audio import resample, to_pcm16, wav_bytes
from opal_talk.tts.base import (
    EngineOptionsError,
    EngineUnavailableError,
    TtsEngine,
    VoiceResolutionError,
)
from opal_talk.tts.chunking import chunk_text
from opal_talk.tts.runtime import EngineRuntime


@dataclass(frozen=True)
class GenerationPlan:
    """A resolved generation request bound to one runtime generation.

    `runtime` is acquired inside plan() and pins the engine generation the request
    was validated against: reconfiguration defers until it drains, so routing,
    option validation, sample-rate resolution and synthesis all observe one
    consistent engine configuration. Ownership transfers to the consumer:
    render()/pcm_stream()/test_voice() release it on every exit path, and
    release() itself is idempotent for early-close or abandoned-plan paths.
    """

    chunks: list[str]
    voice: dict[str, Any]
    engine: TtsEngine
    profile_id: int | None
    speed: float
    volume: float
    output_format: str
    sample_rate: int
    options: dict[str, Any]
    runtime: EngineRuntime

    def release(self) -> None:
        """Detach the bound runtime reference (idempotent)."""
        self.runtime.detach()

    def headers(self, output_format: str | None = None) -> dict[str, str]:
        headers = {
            "X-Opal-Audio-Format": output_format or self.output_format,
            "X-Opal-Sample-Rate": str(self.sample_rate),
            "X-Opal-Channels": "1",
            "X-Opal-Sample-Format": "s16le",
            "X-Opal-Engine-Id": self.engine.engine_id,
            "X-Opal-Voice-Id": self.voice["voice_id"],
        }
        if self.profile_id is not None:
            headers["X-Opal-Profile-Id"] = str(self.profile_id)
        return headers


def _first(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


def translate_engine_error(exc: Exception) -> Exception:
    """Map adapter exceptions onto API errors."""
    if isinstance(exc, ApiError | InvalidRequest):
        return exc
    if isinstance(exc, EngineOptionsError):
        return InvalidRequest.from_issues(["body", "engine_options"], exc.issues, {})
    if isinstance(exc, VoiceResolutionError):
        return Conflict(str(exc), exc.code)
    if isinstance(exc, EngineUnavailableError):
        return Unavailable(str(exc))
    return GenerationFailed(str(exc) or exc.__class__.__name__)


class GenerationService:
    def __init__(self, store: Store, engines: EngineService, settings: SettingsService):
        self.store = store
        self.engines = engines
        self.settings = settings
        # Per-engine serialization is owned by EngineRuntimeManager (engines.runtime).

    # -- resolution -----------------------------------------------------------

    def _check_text(self, text: str) -> list[str]:
        limit = self.settings.effective("max_text_chars")
        if len(text) > limit:
            raise PayloadTooLarge(
                f"text length {len(text)} exceeds the configured maximum of {limit} characters",
                details={"max_text_chars": limit, "text_length": len(text)},
            )
        chunks = chunk_text(text, self.settings.effective("chunk_max_chars"))
        if not chunks:
            raise InvalidRequest.single(["body", "text"], "text contains nothing to speak", input_=text)
        return chunks

    def _route(self, voice: dict[str, Any], engine_override: str | None) -> tuple[TtsEngine, EngineRuntime]:
        """Resolve the target engine and bind its current runtime generation.

        The bound reference pins the configuration generation for the whole request:
        a reconfiguration defers until it drains, so every engine call this request
        makes — validation here, sample-rate resolution and synthesis — observes the
        same engine configuration. Any failure releases the binding before raising.
        """
        voice_id = voice["voice_id"]
        if not voice["enabled"]:
            raise Conflict(f"voice '{voice_id}' is disabled", "voice_disabled", {"voice_id": voice_id})
        engine_id = engine_override or voice["engine_id"]
        engine = self.engines.runtime.get(engine_id)
        if engine is None:
            raise Conflict(f"engine '{engine_id}' is not installed", "engine_not_found", {"engine_id": engine_id})
        try:
            runtime = self.engines.runtime.acquire(engine_id)
        except EngineUnavailableError as exc:
            raise translate_engine_error(exc) from exc
        try:
            if engine_id != voice["engine_id"]:
                mismatch = Conflict(
                    "The requested engine cannot use the resolved voice.",
                    "voice_engine_mismatch",
                    {"voice_id": voice_id, "requested_engine_id": engine_id},
                )
                if engine.validate_options(voice["engine_options"], "voice"):
                    raise mismatch
                try:
                    engine.describe_voice(voice["engine_voice_ref"], voice["engine_options"])
                except (VoiceResolutionError, EngineUnavailableError) as exc:
                    raise mismatch from exc
            if not self.engines.state(engine_id)["enabled"]:
                raise Conflict(f"engine '{engine_id}' is disabled", "engine_disabled", {"engine_id": engine_id})
            available, message = engine.availability()
            if not available:
                raise Unavailable(message or f"engine '{engine_id}' is unavailable", details={"engine_id": engine_id})
            return engine, runtime
        except Exception:
            self.engines.runtime.release(runtime)
            raise

    def _plan(
        self,
        *,
        chunks: list[str],
        voice: dict[str, Any],
        engine: TtsEngine,
        profile: dict[str, Any] | None,
        speed: float,
        volume: float,
        output_format: str,
        sample_rate: int | None,
        option_layers: list[dict[str, Any]],
        runtime: EngineRuntime,
    ) -> GenerationPlan:
        lo, hi = engine.speed_range
        if not lo <= speed <= hi:
            raise Conflict(
                f"speed {speed} is outside the range {lo}..{hi} supported by engine '{engine.engine_id}'",
                "unsupported_speed",
                {"engine_id": engine.engine_id, "speed": speed, "supported": [lo, hi]},
            )
        options: dict[str, Any] = {}
        for layer in option_layers:
            options.update(layer)
        issues = engine.validate_options(options, "voice")
        if issues:
            raise InvalidRequest.from_issues(["body", "engine_options"], issues, options)
        if sample_rate is None:
            try:
                sample_rate = engine.native_sample_rate(voice["engine_voice_ref"], voice["engine_options"])
            except (VoiceResolutionError, EngineUnavailableError) as exc:
                raise translate_engine_error(exc) from exc
        return GenerationPlan(
            chunks=chunks,
            voice=voice,
            engine=engine,
            profile_id=profile["id"] if profile else None,
            speed=float(speed),
            volume=float(volume),
            output_format=output_format,
            sample_rate=int(sample_rate),
            options=options,
            runtime=runtime,
        )

    def plan(self, req: GenerateRequest | GenerateStreamRequest) -> GenerationPlan:
        chunks = self._check_text(req.text)
        if req.profile_id is not None:
            profile = self.store.get_profile(req.profile_id)
            if profile is None:
                raise Conflict(f"profile {req.profile_id} not found", "profile_not_found", {"profile_id": req.profile_id})
        else:
            profile = self.store.active_profile()
        p = profile or {}

        voice_id = _first(req.voice_id, p.get("voice_id"))
        if voice_id is None:
            raise Conflict(
                "no voice could be resolved: supply voice_id or a profile (or activate a profile)", "no_voice"
            )
        voice = self.store.get_voice(voice_id)
        if voice is None:
            raise Conflict(f"voice '{voice_id}' is not registered", "voice_not_found", {"voice_id": voice_id})
        engine, runtime = self._route(voice, req.engine_id)
        try:
            request_issues = engine.validate_options(req.engine_options, "generation")
            if request_issues:
                raise InvalidRequest.from_issues(["body", "engine_options"], request_issues, req.engine_options)

            output_format = _first(
                getattr(req, "output_format", None),
                p.get("output_format"),
                self.settings.effective("default_output_format"),
            )
            # Profile engine_options are engine-specific; they only apply when routing stays on the profile voice's engine.
            profile_options: dict[str, Any] = {}
            if profile:
                profile_voice = voice if voice_id == p["voice_id"] else self.store.get_voice(p["voice_id"])
                if profile_voice and profile_voice["engine_id"] == engine.engine_id:
                    profile_options = p["engine_options"]
            return self._plan(
                chunks=chunks,
                voice=voice,
                engine=engine,
                profile=profile,
                speed=_first(req.speed, p.get("speed"), self.settings.effective("default_speed")),
                volume=_first(req.volume, p.get("volume"), self.settings.effective("default_volume")),
                output_format=output_format,
                sample_rate=_first(req.sample_rate, p.get("sample_rate")),
                option_layers=[voice["engine_options"], profile_options, req.engine_options],
                runtime=runtime,
            )
        except Exception:
            self.engines.runtime.release(runtime)
            raise

    def plan_voice_test(self, voice: dict[str, Any], req: VoiceTestRequest) -> GenerationPlan:
        chunks = self._check_text(req.text)
        engine, runtime = self._route(voice, None)
        try:
            issues = engine.validate_options(req.engine_options, "generation")
            if issues:
                raise InvalidRequest.from_issues(["body", "engine_options"], issues, req.engine_options)
            return self._plan(
                chunks=chunks,
                voice=voice,
                engine=engine,
                profile=None,
                speed=req.speed,
                volume=req.volume,
                output_format=req.output_format,
                sample_rate=req.sample_rate,
                option_layers=[voice["engine_options"], req.engine_options],
                runtime=runtime,
            )
        except Exception:
            self.engines.runtime.release(runtime)
            raise

    # -- synthesis ------------------------------------------------------------

    def pcm_stream(self, plan: GenerationPlan) -> Iterator[bytes]:
        """Yield S16LE mono PCM at `plan.sample_rate`, one block per engine audio chunk.

        Engine exceptions are translated to API errors so callers can report failures that
        occur before the first block as normal JSON errors.
        """
        ref = plan.voice["engine_voice_ref"]
        # The request is already bound to one runtime generation by plan(): synthesis
        # runs against the same configuration snapshot that validation resolved.
        runtime = plan.runtime
        try:
            for text in plan.chunks:
                try:
                    # Hold the inference permit for the whole iterator, but *consume*
                    # chunks incrementally instead of materializing the list.
                    with runtime:
                        for piece in runtime.engine.synthesize(ref, text, plan.speed, plan.options):
                            samples = resample(piece.samples, piece.sample_rate, plan.sample_rate)
                            if samples.size:
                                yield to_pcm16(samples, plan.volume)
                except Exception as exc:  # noqa: BLE001
                    raise translate_engine_error(exc) from exc
        finally:
            plan.release()

    def render(self, plan: GenerationPlan) -> tuple[bytes, str]:
        """Return the completed audio body and its media type.

        Non-streaming responses must buffer the whole output; the accumulated PCM is
        bounded by the `max_audio_bytes` setting — exceeding it aborts early (the
        abandoned stream releases its runtime reference and inference permit) and
        returns 413 instead of growing memory unboundedly. `pcm_stream`, used by
        /generate/stream, emits each block incrementally and has no such bound.
        """
        limit = int(self.settings.effective("max_audio_bytes"))
        total, blocks = 0, []
        try:
            for block in self.pcm_stream(plan):
                total += len(block)
                if total > limit:
                    raise PayloadTooLarge(
                        f"generated audio exceeds the {limit}-byte limit",
                        details={"limit_bytes": limit, "produced_bytes": total},
                    )
                blocks.append(block)
        finally:
            # Covers raises before first iteration and aborts that leave the stream
            # suspended: the bound runtime is released without waiting for GC.
            plan.release()
        pcm = b"".join(blocks)
        if plan.output_format == "wav":
            return wav_bytes(pcm, plan.sample_rate), "audio/wav"
        return pcm, "application/octet-stream"

    def test_voice(self, voice_id: str, req: VoiceTestRequest) -> tuple[GenerationPlan, bytes, str]:
        """Lookup + plan + render for POST /voices/{id}/test, in one call."""
        voice = self.store.get_voice(voice_id)
        if voice is None:
            raise NotFound(f"voice '{voice_id}' not found", "voice_not_found", {"voice_id": voice_id})
        plan = self.plan_voice_test(voice, req)
        data, media_type = self.render(plan)
        return plan, data, media_type
