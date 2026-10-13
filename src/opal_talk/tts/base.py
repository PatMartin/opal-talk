"""Engine-neutral TTS abstractions. Adapters implement `TtsEngine`; nothing here knows about any engine."""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

OPTION_SCOPES = ("engine", "voice", "generation")


@dataclass(frozen=True)
class Issue:
    """A diagnostic about an option, resource or configuration item."""

    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclass
class Report:
    errors: list[Issue] = dataclasses.field(default_factory=list)
    warnings: list[Issue] = dataclasses.field(default_factory=list)
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.errors


class EngineOptionsError(Exception):
    """Engine options were rejected by the adapter."""

    def __init__(self, issues: list[Issue]):
        super().__init__("; ".join(i.message for i in issues))
        self.issues = issues


class VoiceResolutionError(Exception):
    """An opaque engine voice reference could not be resolved on this host."""

    def __init__(self, message: str, code: str = "voice_unresolvable"):
        super().__init__(message)
        self.code = code


class EngineUnavailableError(Exception):
    """The engine runtime or its required resources are not usable."""


class SynthesisError(Exception):
    """The engine failed while generating audio."""


@dataclass(frozen=True)
class VoiceDescription:
    native_sample_rate: int | None
    language: str | None = None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class AudioChunk:
    """Mono float32 samples in [-1, 1] at `sample_rate`."""

    samples: np.ndarray
    sample_rate: int


@dataclass(frozen=True)
class OptionSpec:
    """Declarative description of one engine option key."""

    types: tuple[type, ...]
    scopes: tuple[str, ...]
    check: Callable[[Any], str | None] | None = None
    nullable: bool = False


def check_options(options: Mapping[str, Any], specs: Mapping[str, OptionSpec], scopes: tuple[str, ...]) -> list[Issue]:
    """Validate opaque options against an adapter's declared specs for the given scopes."""
    issues: list[Issue] = []
    for key, value in options.items():
        spec = specs.get(key)
        if spec is None or not set(scopes) & set(spec.scopes):
            allowed = sorted(k for k, s in specs.items() if set(scopes) & set(s.scopes))
            issues.append(Issue("unsupported_option", f"unsupported option '{key}' (allowed: {allowed})", key))
            continue
        if value is None and spec.nullable:
            continue
        # bool is an int subclass; never accept it for numeric options.
        ok = isinstance(value, spec.types) and not (isinstance(value, bool) and bool not in spec.types)
        if not ok:
            names = "/".join(t.__name__ for t in spec.types)
            issues.append(Issue("invalid_option", f"option '{key}' must be of type {names}", key))
            continue
        if spec.check:
            problem = spec.check(value)
            if problem:
                issues.append(Issue("invalid_option", f"option '{key}' {problem}", key))
    return issues


class TtsEngine(ABC):
    """Common interface every TTS engine adapter implements.

    Options are opaque to the rest of the service; only the adapter interprets them.
    Scopes: `engine` (service-wide engine configuration), `voice` (stored with a voice
    registration; also accepts generation keys) and `generation` (profile/request level).
    """

    engine_id: str
    name: str
    description: str | None = None
    option_specs: Mapping[str, OptionSpec] = {}
    speed_range: tuple[float, float] = (0.25, 4.0)

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    @property
    def options(self) -> dict[str, Any]:
        return dict(self._options)

    def default_options(self) -> dict[str, Any]:
        return {}

    def validate_options(self, options: Mapping[str, Any], scope: str) -> list[Issue]:
        # Voice registrations may also carry generation defaults.
        scopes = ("voice", "generation") if scope == "voice" else (scope,)
        return check_options(options, self.option_specs, scopes)

    def configure(self, options: Mapping[str, Any]) -> None:
        """Apply engine-scope options (already validated). Adapters drop caches here."""
        self._options = dict(options)

    @abstractmethod
    def availability(self) -> tuple[bool, str | None]:
        """Return (available, message) for the runtime and current engine configuration."""

    def metadata(self) -> dict[str, Any]:
        return {}

    def validate(self) -> Report:
        report = Report()
        for issue in self.validate_options(self._options, "engine"):
            report.errors.append(issue)
        available, message = self.availability()
        if not available:
            report.errors.append(Issue("engine_unavailable", message or "engine is unavailable"))
        report.details.update(self.metadata())
        return report

    @abstractmethod
    def describe_voice(self, ref: str, voice_options: Mapping[str, Any]) -> VoiceDescription:
        """Resolve an opaque voice reference. Raises VoiceResolutionError / EngineUnavailableError."""

    def native_sample_rate(self, ref: str, voice_options: Mapping[str, Any]) -> int:
        rate = self.describe_voice(ref, voice_options).native_sample_rate
        if not rate:
            raise VoiceResolutionError(f"native sample rate of voice '{ref}' is unknown")
        return rate

    def validate_voice(self, ref: str, voice_options: Mapping[str, Any]) -> Report:
        report = Report()
        report.errors.extend(self.validate_options(voice_options, "voice"))
        available, message = self.availability()
        if not available:
            report.errors.append(Issue("engine_unavailable", message or "engine is unavailable"))
            return report
        try:
            desc = self.describe_voice(ref, voice_options)
        except VoiceResolutionError as exc:
            report.errors.append(Issue(exc.code, str(exc), "engine_voice_ref"))
        except EngineUnavailableError as exc:
            report.errors.append(Issue("engine_unavailable", str(exc)))
        else:
            report.details.update({"native_sample_rate": desc.native_sample_rate, "language": desc.language})
        return report

    @abstractmethod
    def synthesize(
        self, ref: str, text: str, speed: float, options: Mapping[str, Any]
    ) -> Iterator[AudioChunk]:
        """Yield audio for one text chunk. `options` holds merged voice+profile+request options.

        Implementations must raise EngineOptionsError for unusable options before yielding audio.
        """
