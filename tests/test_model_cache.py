"""ModelCache: single-flight loads, LRU bounds, use-counted entries, clear()."""

from __future__ import annotations

import threading
import time

import pytest

from opal_talk.tts.cache import ModelCache


class Closable:
    """Model stand-in that records disposal."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def test_single_flight_one_loader_call_for_concurrent_waiters():
    cache = ModelCache(capacity=4)
    gate = threading.Event()
    calls: list[str] = []

    def load():
        calls.append("load")
        gate.wait(5)
        return "voice"

    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(cache.acquire("k", load).value)) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(5)
    assert calls == ["load"] and results == ["voice"] * 6
    for e in list(cache._entries.values()):
        cache.release(e)


def test_different_keys_load_concurrently():
    cache = ModelCache(capacity=4)
    both_entered = threading.Barrier(2, timeout=5)

    def load(name):
        def inner():
            both_entered.wait()  # deadlocks if loads serialize on a global lock
            return name
        return inner

    out: dict[str, str] = {}
    t1 = threading.Thread(target=lambda: out.update({"a": cache.acquire("a", load("va")).value}))
    t2 = threading.Thread(target=lambda: out.update({"b": cache.acquire("b", load("vb")).value}))
    t1.start(), t2.start()
    t1.join(5), t2.join(5)
    assert out == {"a": "va", "b": "vb"}


def test_lru_bound_evicts_oldest_idle_entry():
    cache = ModelCache(capacity=2)
    entries = {k: cache.acquire(k, lambda k=k: f"v-{k}") for k in "abc"}
    for e in entries.values():
        cache.release(e)
    e = cache.acquire("d", lambda: "v-d")  # a new insert trims idle entries to the bound
    assert list(cache._entries) == ["c", "d"]  # 'a' and 'b' (LRU, idle) dropped
    cache.release(e)


def test_entries_in_use_survive_over_capacity():
    cache = ModelCache(capacity=1)
    held = cache.acquire("a", lambda: "va")
    cache.release(cache.acquire("b", lambda: "vb"))  # 'a' pinned, 'b' idle
    cache.release(cache.acquire("c", lambda: "vc"))  # insert trims: 'b' evicted, pinned 'a' survives
    assert "a" in cache._entries and "b" not in cache._entries
    # The bound tightens again once 'a' drains.
    cache.release(held)
    cache.release(cache.acquire("d", lambda: "vd"))
    assert "a" not in cache._entries


def test_failed_load_propagates_to_waiters_and_does_not_poison():
    cache = ModelCache(capacity=2)
    gate = threading.Event()
    errors: list[Exception] = []

    def bad_load():
        gate.wait(5)
        raise RuntimeError("model broken")

    threads = [
        threading.Thread(target=lambda: pytest.raises(RuntimeError))
        for _ in range(3)
    ]

    def attempt():
        try:
            cache.acquire("k", bad_load)
        except RuntimeError as e:
            errors.append(e)

    threads = [threading.Thread(target=attempt) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(errors) == 3
    # Nothing cached: a fresh attempt retries cleanly.
    assert cache.acquire("k", lambda: "ok").value == "ok"


def test_clear_stales_entries_but_lets_active_users_drain():
    cache = ModelCache(capacity=4)
    held = cache.acquire("a", lambda: "v1")
    cache.clear()
    # Lookup misses now -> a new generation of the entry is loaded on demand.
    fresh = cache.acquire("a", lambda: "v2")
    assert fresh.value == "v2" and fresh is not held
    assert held.value == "v1"  # the pinned entry is still usable
    cache.release(held)
    cache.release(fresh)


def test_stale_load_racing_clear_is_not_served():
    """A load that completes after clear() is stale-marked; the caller still gets a
    value (via a post-clear reload), and the pre-clear entry is gone."""
    cache = ModelCache(capacity=4)
    gate = threading.Event()
    calls: list[int] = []

    def slow_load():
        calls.append(1)
        gate.wait(5)
        return f"model{len(calls)}"

    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(cache.acquire("a", slow_load).value))
    t.start()
    time.sleep(0.1)
    cache.clear()
    gate.set()
    t.join(5)
    # The caller that triggered the load still gets its (released) result, but the
    # stale-marked entry is not served to later acquirers: the next acquire reloads.
    assert len(calls) == 1 and result == ["model1"]
    for e in list(cache._entries.values()):
        cache.release(e)
    assert cache.acquire("a", slow_load).value == "model2" and len(calls) == 2


# -- stale-entry disposal ----------------------------------------------------------


def test_stale_entry_disposed_after_replacement():
    """An evicted-but-held entry must be disposed on release even after a fresh
    entry with the same key has replaced it in the cache dict."""
    cache = ModelCache(capacity=4)
    old = Closable("old")
    held = cache.acquire("k", lambda: old)
    cache.clear()  # marks the held entry evicted but pinned
    fresh = cache.acquire("k", lambda: Closable("new"))
    assert cache._entries["k"] is fresh and fresh.value.name == "new"
    cache.release(held)
    assert old.closed == 1  # regression: leaked when the dict slot was already replaced
    cache.release(fresh)
    assert fresh.value.closed == 0  # fresh generation is not affected


def test_evicted_entry_disposed_exactly_once():
    """Disposal fires once on the last release; over-release is a no-op."""
    cache = ModelCache(capacity=4)
    obj = Closable("m")
    held = cache.acquire("k", lambda: obj)
    cache.clear()
    cache.release(held)
    cache.release(held)  # must not dispose twice or drive refs negative
    assert obj.closed == 1 and held.refs == 0
    assert "k" not in cache._entries


def test_stale_load_result_disposed_on_release():
    """A load racing clear() produces a stale entry; releasing it disposes it."""
    cache = ModelCache(capacity=4)
    gate = threading.Event()
    stale = Closable("stale")

    def slow_load():
        gate.wait(5)
        return stale

    out: list = []
    t = threading.Thread(target=lambda: out.append(cache.acquire("a", slow_load)))
    t.start()
    time.sleep(0.1)
    cache.clear()
    gate.set()
    t.join(5)
    entry = out[0]
    assert entry.value is stale and entry.evicted
    cache.release(entry)
    assert stale.closed == 1


def test_clear_during_replacement_disposes_both_generations():
    """clear() between two same-key generations: both held copies drain and dispose."""
    cache = ModelCache(capacity=4)
    first, second = Closable("first"), Closable("second")
    held1 = cache.acquire("k", lambda: first)
    cache.clear()  # first stale, still held
    held2 = cache.acquire("k", lambda: second)  # replacement generation
    cache.clear()  # second stale while held too
    cache.release(held1)
    cache.release(held2)
    assert first.closed == 1 and second.closed == 1
    assert "k" not in cache._entries


def test_clear_during_load_and_usage_drains_cleanly():
    """clear() while a same-key load and a held entry are both in flight; every
    stale generation is still disposed on its last release."""
    cache = ModelCache(capacity=4)
    old = Closable("old")
    held = cache.acquire("k", lambda: old)
    gate = threading.Event()
    new = Closable("new")

    def slow_load():
        gate.wait(5)
        return new

    cache.clear()  # entry evicted but pinned
    out: list = []
    t = threading.Thread(target=lambda: out.append(cache.acquire("k", slow_load)))
    t.start()
    time.sleep(0.1)
    cache.clear()  # second clear: the in-flight load's result will be stale-marked
    gate.set()
    t.join(5)
    entry = out[0]
    fresh = cache.acquire("k", lambda: Closable("fresh"))  # replaces the dict slot again
    cache.release(held)
    cache.release(entry)
    cache.release(fresh)
    assert old.closed == 1 and new.closed == 1
    assert fresh.value.closed == 0
    assert cache._entries["k"] is fresh  # the live generation stays cached


def test_failed_load_leaves_no_leaked_state():
    cache = ModelCache(capacity=2)

    with pytest.raises(RuntimeError):
        cache.acquire("k", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert "k" not in cache._entries and not cache._loading
    obj = Closable("ok")
    entry = cache.acquire("k", lambda: obj)
    assert entry.value is obj and entry.refs == 1
    cache.clear()
    cache.release(entry)
    assert obj.closed == 1 and "k" not in cache._entries
