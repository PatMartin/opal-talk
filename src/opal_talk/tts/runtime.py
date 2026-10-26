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

# Reserved engine option handled at runtime level (never passed to adapters):
# how many synthesize() calls may run concurrently on one engine. Default 1
# preserves the old serialized behavior.
RUNTIME_OPTION = "max_concurrent_inferences"


def split_runtime_options(options: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    """Split adapter options from the runtime-level `max_concurrent_inferences` key."""
    rest = {k: v for k, v in options.items() if k != RUNTIME_OPTION}
    raw = options.get(RUNTIME_OPTION)
    return rest, (None if raw is None else int(raw))


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
    sem: threading.BoundedSemaphore
    permits: int
    waiters: int = 0
    refs: int = 0
    retired: bool = False
    superseded: bool = False
    pending: _RuntimeRecord | None = None
    prev: _RuntimeRecord | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    failed: BaseException | None = None


@dataclass(eq=False)
class EngineRuntime:
    """A usage reference bound to one runtime record (engine instance + generation).

    Enter it (`with rt:`) around adapter calls to take an inference permit — at most
    `record.permits` run concurrently, and at most `permits` more may queue before
    `EngineUnavailableError` rejects the request (bounded backpressure).
    `release()` detaches the reference; detaching is idempotent.
    """

    engine: TtsEngine
    _record: _RuntimeRecord
    _mgr: EngineRuntimeManager
    _held: bool = False
    _detached: bool = False

    def acquire(self) -> EngineRuntime:
        self._mgr._enter(self._record)
        self._held = True
        return self

    def release(self) -> None:
        if self._held:
            self._held = False
            self._record.sem.release()

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
            rec = _RuntimeRecord(
                engine, generation=0, options=None,
                sem=threading.BoundedSemaphore(1), permits=1,
            )
            rec.ready.set()
            self._records[engine.engine_id] = rec

    @staticmethod
    def _limit(engine: TtsEngine, options: dict[str, Any] | None) -> int:
        """Effective concurrency: configured limit clamped to the adapter's declared cap."""
        _, configured = split_runtime_options(options or {})
        declared = int(getattr(engine, "max_concurrent_inferences", 1))
        return max(1, min(configured or 1, declared))

    def _enter(self, rec: _RuntimeRecord) -> None:
        """Take an inference permit; reject when the wait queue is already full."""
        with self._guard:
            if rec.waiters >= rec.permits:
                raise EngineUnavailableError(
                    f"engine '{rec.engine.engine_id}' is at capacity "
                    f"({rec.permits} in flight, {rec.waiters} waiting)"
                )
            rec.waiters += 1
        try:
            rec.sem.acquire()
        finally:
            with self._guard:
                rec.waiters -= 1

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
        raises `EngineUnavailableError` if that application failed. A binding that
        lands on a superseded generation is dropped and re-bound to the current
        record, so every waiter either proceeds or gets an explicit error.
        """
        while True:
            with self._guard:
                rec = self._record(engine_id)
                rec.refs += 1
            rec.ready.wait()
            if rec.failed is None:
                return EngineRuntime(rec.engine, rec, self)
            superseded = rec.superseded
            self.release(EngineRuntime(rec.engine, rec, self))
            if not superseded:
                raise EngineUnavailableError(
                    f"engine '{engine_id}' reconfiguration failed: {rec.failed}"
                ) from rec.failed
            # Superseded generation: re-bind the current record instead of failing.

    def release(self, runtime: EngineRuntime) -> None:
        """Detach a usage reference; applies a deferred reconfiguration on drain."""
        runtime.release()
        if runtime._detached:
            return
        runtime._detached = True
        rec = runtime._record
        with self._guard:
            if rec.refs > 0:
                rec.refs -= 1
            pending = rec.pending if rec.retired and rec.refs == 0 else None
            if pending is not None:
                rec.pending = None
        if pending is not None:
            # Outside the guard: configure may be slow; pending.ready gates use anyway.
            try:
                rec.engine.configure(split_runtime_options(pending.options or {})[0])
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

    @staticmethod
    def _deepest_engaged(rec: _RuntimeRecord) -> _RuntimeRecord | None:
        """Walk the predecessor chain for the oldest generation that is still live
        (bound references) or whose configuration is still being applied (`ready`
        unset). Generations share one engine instance, so any of them being live
        means an immediate configure() would interrupt an in-flight request."""
        target = None
        while rec is not None:
            if rec.refs > 0 or not rec.ready.is_set():
                target = rec
            rec = rec.prev
        return target

    def swap(self, engine_id: str, options: dict[str, Any]) -> None:
        """Install a new configuration generation for `engine_id`.

        Engine idle (no generation in the chain is live or activating): applies
        immediately — a raise leaves the previous record active and untouched.
        Engine in use: defers onto the deepest engaged generation; an earlier
        pending generation is superseded deterministically — the newest deferred
        configuration wins and the superseded one's waiters re-bind the current
        record instead of failing or blocking forever.
        """
        adapter_options, _ = split_runtime_options(options)
        with self._guard:
            old = self._record(engine_id)
            target = self._deepest_engaged(old)
            if target is None:
                old.engine.configure(adapter_options)  # raises with nothing swapped
            permits = self._limit(old.engine, options)
            new = _RuntimeRecord(
                engine=old.engine,
                generation=old.generation + 1,
                options=dict(options),
                sem=threading.BoundedSemaphore(permits),
                permits=permits,
                # Dead middle generations are skipped: only the engaged prefix of
                # the chain can ever matter to a future drain.
                prev=target,
            )
            if target is None:
                new.ready.set()
            else:
                target.retired = True
                superseded = target.pending
                target.pending = new
                if superseded is not None:
                    superseded.retired = True
                    superseded.superseded = True
                    superseded.failed = EngineUnavailableError(
                        f"engine '{engine_id}' configuration superseded"
                    )
                    superseded.ready.set()
            self._records[engine_id] = new

    def configure(self, engine_id: str, options: dict[str, Any]) -> None:
        """Lifecycle shorthand: install a new configuration generation (no-op for
        unknown ids)."""
        if engine_id not in self._records:
            return
        self.swap(engine_id, options)
