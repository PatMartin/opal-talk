"""ModelCache: single-flight loads, LRU bounds, use-counted entries, clear()."""

from __future__ import annotations

import threading
import time

import pytest

from opal_talk.tts.cache import ModelCache


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
