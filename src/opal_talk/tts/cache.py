"""Single-flight LRU cache for heavyweight model/voice objects.

Concurrent `acquire()` calls for the same uncached key run the loader exactly once;
waiters share the outcome — a failed load propagates to all of them and leaves no
poisoned state, so the next caller retries. Loads for *different* keys proceed
concurrently (no global lock is held while loading). Entries with active users are
never evicted: `clear()` (used on reconfiguration) marks everything stale so lookups
miss, but pinned entries stay usable until their last `release()`.
"""

from __future__ import annotations

import contextlib
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(eq=False)
class CacheEntry:
    """A held reference to a cached object. Pass to `release()` when done."""

    key: Any
    value: Any
    refs: int = 0
    evicted: bool = False


class _Loading:
    """One in-flight load: waiters block on `event`, then see `error` or a cache hit."""

    def __init__(self, gen: int) -> None:
        self.gen = gen
        self.event = threading.Event()
        self.error: BaseException | None = None

    def finish(self, error: BaseException | None) -> None:
        self.error = error
        self.event.set()


@dataclass
class ModelCache:
    """Bounded LRU cache with single-flight loads and use-counted entries."""

    capacity: Callable[[], int] | int
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _entries: OrderedDict[Any, CacheEntry] = field(default_factory=OrderedDict)
    _loading: dict[Any, _Loading] = field(default_factory=dict)
    _gen: int = 0

    def _capacity(self) -> int:
        return self.capacity() if callable(self.capacity) else self.capacity

    @staticmethod
    def _dispose(entry: CacheEntry) -> None:
        close = getattr(entry.value, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):  # disposal is best-effort
                close()

    def acquire(self, key: Any, load: Callable[[], T]) -> CacheEntry:
        """Return a held `CacheEntry` for `key`, loading once if uncached.

        `load` runs outside the cache lock. Every waiter for the same key shares the
        one in-flight load; a loader failure propagates to all waiters and is dropped,
        so a later caller starts a fresh attempt.
        """
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and not entry.evicted:
                    entry.refs += 1
                    self._entries.move_to_end(key)
                    return entry
                pending = self._loading.get(key)
                if pending is None:
                    pending = self._loading[key] = _Loading(self._gen)
                    owner = True
                else:
                    owner = False
            if not owner:
                pending.event.wait()
                if pending.error is not None:
                    raise pending.error
                continue  # load finished: re-check the (now populated) cache
            try:
                value = load()
            except BaseException as exc:
                with self._lock:
                    self._loading.pop(key, None)
                pending.finish(exc)
                raise
            with self._lock:
                self._loading.pop(key, None)
                # Held at birth: the fresh entry can never be its own trim victim.
                entry = CacheEntry(key, value, refs=1)
                if pending.gen != self._gen:
                    entry.evicted = True  # stale: a clear() raced this load
                self._entries[key] = entry
                victims = self._trim_locked()
            pending.finish(None)
            for victim in victims:
                self._dispose(victim)
            return entry

    def release(self, entry: CacheEntry) -> None:
        """Drop a held reference; a stale entry is disposed at zero users.

        Disposal follows the entry, not the dict slot: an evicted entry whose key
        a fresh generation already replaced is still disposed here exactly once.
        """
        dispose = False
        with self._lock:
            if entry.refs <= 0:
                return  # over-release: nothing held
            entry.refs -= 1
            if entry.evicted and entry.refs == 0:
                if self._entries.get(entry.key) is entry:
                    del self._entries[entry.key]
                dispose = True
        if dispose:
            self._dispose(entry)

    def clear(self) -> None:
        """Mark every entry stale: lookups miss and reload, active users drain."""
        dispose: list[CacheEntry] = []
        with self._lock:
            self._gen += 1
            for key, entry in list(self._entries.items()):
                entry.evicted = True
                if entry.refs == 0:
                    del self._entries[key]
                    dispose.append(entry)
        for entry in dispose:
            self._dispose(entry)

    def _trim_locked(self) -> list[CacheEntry]:
        """Evict least-recently-used idle entries until within capacity.

        Entries with active users are never evicted; the bound can be exceeded while
        everything is in use and tightens again as entries are released. Callers
        dispose the returned victims *outside* the lock.
        """
        evict = [k for k, e in self._entries.items() if e.refs == 0]
        drop = max(0, len(self._entries) - self._capacity())
        return [self._entries.pop(k) for k in evict[:drop]]
