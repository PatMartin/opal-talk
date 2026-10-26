"""Engine runtime ownership: holds the initialized adapters and coordinates their lifecycle.

`EngineRegistry` decides *which* engines exist; `EngineRuntimeManager` owns the live
`TtsEngine` instances. Read-only access (option specs, describe_voice, validate_options,
availability) stays unlocked exactly as before — the manager adds no new locking; it
centralizes the per-engine serialization that GenerationService used to keep privately.

Usage:
    with manager.using("piper") as engine:   # serialized per-engine usage
        pieces = engine.synthesize(...)
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opal_talk.tts.base import TtsEngine
from opal_talk.tts.registry import EngineRegistry


@dataclass(eq=False)
class EngineRuntime:
    """A held usage reference to one managed engine instance.

    While held, the engine's serialization lock is owned — callers doing real work
    with the adapter keep the runtime for the whole operation. Release exactly once.
    """

    engine: TtsEngine
    _lock: threading.Lock
    _held: bool = False

    def acquire(self) -> EngineRuntime:
        self._lock.acquire()
        self._held = True
        return self

    def release(self) -> None:
        if self._held:
            self._held = False
            self._lock.release()

    def __enter__(self) -> EngineRuntime:
        return self if self._held else self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class EngineRuntimeManager:
    """Owns the managed `TtsEngine` instances and their lifecycle.

    - `get` / `ids` / `all` — lookup of managed instances (metadata access, unlocked).
    - `acquire` / `release` / `using` — serialized usage references, one per engine.
    - `configure` — lifecycle coordination: pushes engine-scope options into the
      managed instance (adapters drop caches there, as before).
    """

    def __init__(self, registry: EngineRegistry):
        self._registry = registry
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    def get(self, engine_id: str) -> TtsEngine | None:
        return self._registry.get(engine_id)

    def ids(self) -> list[str]:
        return self._registry.ids()

    def all(self) -> list[TtsEngine]:
        return self._registry.all()

    def acquire(self, engine_id: str) -> EngineRuntime:
        """Obtain a serialized usage reference for `engine_id` (KeyError if unknown)."""
        engine = self._registry.get(engine_id)
        if engine is None:
            raise KeyError(f"engine '{engine_id}' is not managed by this runtime")
        return EngineRuntime(engine, self._locks[engine_id]).acquire()

    def release(self, runtime: EngineRuntime) -> None:
        """Release a usage reference previously returned by `acquire`."""
        runtime.release()

    @contextmanager
    def using(self, engine_id: str) -> Iterator[TtsEngine]:
        """Context sugar around acquire/release; yields the managed engine instance."""
        runtime = self.acquire(engine_id)
        try:
            yield runtime.engine
        finally:
            self.release(runtime)

    def configure(self, engine_id: str, options: dict[str, Any]) -> None:
        """Lifecycle: push engine-scope options into the managed instance, if present."""
        engine = self._registry.get(engine_id)
        if engine is not None:
            engine.configure(options)
