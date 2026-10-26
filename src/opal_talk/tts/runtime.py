"""Engine runtime ownership: holds the managed adapters and coordinates their lifecycle.

`EngineRegistry` decides *which* engines exist; `EngineRuntimeManager` owns the live
`TtsEngine` instances behind versioned `_RuntimeRecord`s — each record is a snapshot of
the engine's runtime configuration. A generation request binds to one record via
`acquire()`, so it always sees a consistent configuration even if reconfiguration lands
mid-request: the new options are applied to the instance only after the old generation's
last usage reference drains (`release`). Read-only access (option specs, describe_voice,
validate_options, availability) stays unlocked — adapter calls keep the same per-instance
serialization the service had before.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opal_talk.tts.base import EngineUnavailableError, TtsEngine
from opal_talk.tts.registry import EngineRegistry


@dataclass(eq=False)
class _RuntimeRecord:
    """One configuration generation for an engine: what bound requests see.

    `options` is the snapshot this generation represents. `pending` points at the
    successor generation whose configure() still waits for this generation to drain.
    `ready` gates records whose options are not applied to the instance yet.
    """

    engine: TtsEngine
    generation: int
    options: dict[str, Any] | None
    lock: threading.Lock
    refs: int = 0
    retired: bool = False
    pending: _RuntimeRecord | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    failed: BaseException | None = None


@dataclass(eq=False)
class EngineRuntime:
    """A usage reference bound to one runtime record (engine instance + generation).

    Enter it (`with rt:`) around adapter calls to serialize work on that instance —
    the same per-engine locking the service had before. `release()` detaches the
    reference; release exactly once.
    """

    engine: TtsEngine
    _record: _RuntimeRecord
    _held: bool = False

    def acquire(self) -> EngineRuntime:
        self._record.lock.acquire()
        self._held = True
        return self

    def release(self) -> None:
        if self._held:
            self._held = False
            self._record.lock.release()

    def __enter__(self) -> EngineRuntime:
        return self if self._held else self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class EngineRuntimeManager:
    """Owns the managed `TtsEngine` instances and their lifecycle.

    - `get` / `ids` / `all` — lookup of managed instances (metadata access, unlocked).
    - `acquire` / `release` / `using` — usage references bound to one record; a
      reconfigured record waits for the previous generation's references to drain
      before its options are applied.
    - `swap` / `configure` — lifecycle: install a new configuration generation. With
      no active users it applies immediately (failures raise with nothing swapped);
      with active users it defers until they finish.
    """

    def __init__(self, registry: EngineRegistry):
        self._guard = threading.Lock()
        self._records: dict[str, _RuntimeRecord] = {}
        for engine in registry.all():
            rec = _RuntimeRecord(engine, generation=0, options=None, lock=threading.Lock())
            rec.ready.set()
            self._records[engine.engine_id] = rec

    def _record(self, engine_id: str) -> _RuntimeRecord:
        try:
            return self._records[engine_id]
        except KeyError:
            raise KeyError(f"engine '{engine_id}' is not managed by this runtime") from None

    def get(self, engine_id: str) -> TtsEngine | None:
        rec = self._records.get(engine_id)
        return rec.engine if rec else None

    def ids(self) -> list[str]:
        return sorted(self._records)

    def all(self) -> list[TtsEngine]:
        return [self._records[k].engine for k in self.ids()]

    def acquire(self, engine_id: str) -> EngineRuntime:
        """Bind a usage reference to the current runtime record (KeyError if unknown).

        Blocks while the record's configuration is still pending application, and
        raises `EngineUnavailableError` if that application failed.
        """
        with self._guard:
            rec = self._record(engine_id)
            rec.refs += 1
        rec.ready.wait()
        if rec.failed is not None:
            self.release(EngineRuntime(rec.engine, rec))
            raise EngineUnavailableError(
                f"engine '{engine_id}' reconfiguration failed: {rec.failed}"
            ) from rec.failed
        return EngineRuntime(rec.engine, rec)

    def release(self, runtime: EngineRuntime) -> None:
        """Detach a usage reference; applies a deferred reconfiguration on drain."""
        runtime.release()
        rec = runtime._record
        with self._guard:
            rec.refs -= 1
            pending = rec.pending if rec.retired and rec.refs == 0 else None
            if pending is not None:
                rec.pending = None
        if pending is not None:
            # Outside the guard: configure may be slow; pending.ready gates use anyway.
            try:
                rec.engine.configure(pending.options or {})
            except Exception as exc:  # noqa: BLE001 - surface it to future acquirers
                pending.failed = exc
            pending.ready.set()

    @contextmanager
    def using(self, engine_id: str) -> Iterator[TtsEngine]:
        """Context sugar around acquire/lock/release; yields the bound engine instance."""
        runtime = self.acquire(engine_id)
        try:
            with runtime:
                yield runtime.engine
        finally:
            self.release(runtime)

    # -- lifecycle -------------------------------------------------------------

    def swap(self, engine_id: str, options: dict[str, Any]) -> None:
        """Install a new configuration generation for `engine_id`.

        No active users: applies immediately — a raise leaves the previous record
        active and untouched. Active users: defers until they release.
        """
        with self._guard:
            old = self._record(engine_id)
            if old.refs == 0:
                old.engine.configure(options)  # raises with nothing swapped
            new = _RuntimeRecord(
                engine=old.engine,
                generation=old.generation + 1,
                options=dict(options),
                lock=old.lock,  # same instance -> same serialization lock
            )
            if old.refs == 0:
                new.ready.set()
            else:
                old.retired = True
                if old.pending is not None:
                    # A second reconfigure supersedes the pending generation: wake its
                    # waiters with a failure instead of leaving them blocked forever.
                    old.pending.failed = EngineUnavailableError(
                        f"engine '{engine_id}' configuration superseded"
                    )
                    old.pending.ready.set()
                old.pending = new
            self._records[engine_id] = new

    def configure(self, engine_id: str, options: dict[str, Any]) -> None:
        """Lifecycle shorthand: install a new configuration generation (no-op for
        unknown ids)."""
        if engine_id not in self._records:
            return
        self.swap(engine_id, options)
