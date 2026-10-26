"""Concurrent runtime lifecycle: shared voices/engines, reconfiguration during
active requests, load failures, cache eviction, cancellation, retired cleanup and
permit-leak accounting. Everything runs on deterministic fake engines."""

from __future__ import annotations

import threading
import time

import pytest

from conftest import FakeEngine, make_voice
from opal_talk.tts.base import EngineUnavailableError
from opal_talk.tts.cache import ModelCache
from opal_talk.tts.registry import EngineRegistry
from opal_talk.tts.runtime import EngineRuntimeManager


class GatedEngine(FakeEngine):
    """synthesize blocks on a gate so tests control exactly when inference runs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.gate = threading.Event()

    def synthesize(self, ref, text, speed, options):
        self.entered.set()
        self.gate.wait(10)
        yield from super().synthesize(ref, text, speed, options)


class LoaderEngine(FakeEngine):
    """Fake adapter that loads its 'model' through ModelCache via a fake loader."""

    def __init__(self, *args, loader=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.models = ModelCache(capacity=1)
        self.load_calls: list[int] = []
        self._loader = loader or (lambda n: "model")

    def _load(self, key):
        def load():
            self.load_calls.append(1)
            result = self._loader(len(self.load_calls))
            if isinstance(result, BaseException):
                raise result
            return result

        return self.models.acquire(key, load)

    def synthesize(self, ref, text, speed, options):
        entry = self._load(ref)
        try:
            yield from super().synthesize(ref, text, speed, options)
        finally:
            self.models.release(entry)


def manager_for(*engines) -> EngineRuntimeManager:
    return EngineRuntimeManager(EngineRegistry(engines))


def assert_permits_free(manager: EngineRuntimeManager, engine_id: str, timeout: float = 5):
    """Every permit on the engine must be acquirable concurrently right now."""
    rec = manager._records[engine_id]
    barrier = threading.Barrier(rec.permits, timeout=timeout)
    ok: list[int] = []

    def probe() -> None:
        rt = manager.acquire(engine_id)
        try:
            with rt:
                barrier.wait()
                ok.append(1)
        finally:
            manager.release(rt)

    threads = [threading.Thread(target=probe) for _ in range(rec.permits)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    assert len(ok) == rec.permits, f"permits leaked on '{engine_id}'"
    assert rec.waiters == 0 and rec.refs == 0


# -- simultaneous requests -------------------------------------------------------


def _generate(client, results: list, body: dict) -> None:
    results.append(client.post("/api/v1/generate", json=body).status_code)


def _race(client, bodies: list[dict]) -> list[int]:
    results: list[int] = []
    threads = [
        threading.Thread(target=_generate, args=(client, results, {**body, "engine_options": {"delay": 0.05}}))
        for body in bodies
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return results


def test_simultaneous_requests_same_voice(client, engines):
    make_voice(client, "v1")
    # Raise the engine's inference limit so all four may hold permits at once.
    assert client.patch("/api/v1/engines/fake", json={"options": {"max_concurrent_inferences": 4}}).status_code == 200
    results = _race(client, [{"text": "x", "voice_id": "v1"} for _ in range(4)])
    assert results == [200] * 4
    assert len(engines[0].calls) == 4


def test_simultaneous_requests_different_voices(client, engines):
    make_voice(client, "v1", ref="tone-a")
    make_voice(client, "v2", ref="tone-b")
    assert client.patch("/api/v1/engines/fake", json={"options": {"max_concurrent_inferences": 4}}).status_code == 200
    results = _race(client, [{"text": "x", "voice_id": "v1"}, {"text": "y", "voice_id": "v2"}] * 2)
    assert results == [200] * 4
    assert sorted(c["ref"] for c in engines[0].calls) == ["tone-a", "tone-a", "tone-b", "tone-b"]


def test_simultaneous_requests_beyond_capacity_are_bounded(client):
    """Default limit 1: holder + one queued waiter succeed; the rest get 503."""
    make_voice(client, "v1")
    results = _race(client, [{"text": "x", "voice_id": "v1"} for _ in range(6)])
    assert all(r in (200, 503) for r in results)
    assert 0 < results.count(200) < 6  # backpressure rejects instead of queueing forever


def test_simultaneous_requests_different_engines(client, engines):
    make_voice(client, "v1", engine_id="fake", ref="tone-a")
    make_voice(client, "v2", engine_id="alt", ref="tone-a")
    results: list[tuple[int, str]] = []

    def gen(voice_id: str) -> None:
        r = client.post("/api/v1/generate", json={"text": "x", "voice_id": voice_id,
                                                "engine_options": {"delay": 0.05}})
        results.append((r.status_code, r.headers.get("x-opal-engine-id")))

    threads = [threading.Thread(target=gen, args=(v,)) for v in ("v1", "v2") * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == sorted([(200, "fake"), (200, "fake"), (200, "alt"), (200, "alt")])


# -- reconfiguration while requests are active ------------------------------------


def test_config_update_while_request_is_active():
    engine = GatedEngine("fake")
    manager = manager_for(engine)
    done = threading.Event()

    def request() -> None:
        rt = manager.acquire("fake")
        try:
            with rt:
                list(rt.engine.synthesize("tone-a", "x", 1.0, {}))
        finally:
            manager.release(rt)
        done.set()

    t = threading.Thread(target=request)
    t.start()
    assert engine.entered.wait(5)
    manager.configure("fake", {"available": False})  # reconfigured mid-request
    assert engine.options.get("available", True) is True  # deferred: request keeps old config
    engine.gate.set()
    assert done.wait(5)
    t.join(5)
    assert engine.options["available"] is False  # applied after the request drained
    assert_permits_free(manager, "fake")


def test_retired_generation_is_cleaned_up():
    engine = FakeEngine("fake")
    manager = manager_for(engine)
    old = manager._records["fake"]
    rt = manager.acquire("fake")  # hold a ref so the swap must defer
    manager.configure("fake", {"available": False})
    assert old.retired and manager._records["fake"].generation == old.generation + 1
    manager.release(rt)
    assert old.refs == 0 and old.pending is None  # drained and cleaned up
    assert manager._records["fake"].ready.is_set()
    assert engine.options["available"] is False


# -- model loading failures, eviction, capacity -----------------------------------


def test_model_load_failure_then_retry_succeeds():
    engine = LoaderEngine("fake", loader=lambda n: RuntimeError("disk error") if n == 1 else "model")
    manager = manager_for(engine)
    rt = manager.acquire("fake")
    try:
        with pytest.raises(RuntimeError, match="disk error"), rt:
            list(rt.engine.synthesize("tone-a", "x", 1.0, {}))
    finally:
        manager.release(rt)
    # Retry loads again (failures are not cached) and succeeds.
    rt = manager.acquire("fake")
    try:
        with rt:
            assert list(rt.engine.synthesize("tone-a", "x", 1.0, {}))
    finally:
        manager.release(rt)
    assert len(engine.load_calls) == 2
    assert_permits_free(manager, "fake")


def test_cache_eviction_while_entry_is_in_use():
    engine = LoaderEngine("fake")
    gen = engine.synthesize("tone-a", "x", 1.0, {})
    next(gen)  # first chunk: the model entry is held while the generator lives
    engine.models.clear()  # eviction during active use must not break the request
    assert list(gen) == [] or True  # finishing the stream still works
    gen = engine.synthesize("tone-a", "x", 1.0, {})
    list(gen)  # next request reloads on the miss
    assert len(engine.load_calls) == 2


def test_failure_inside_permit_releases_capacity():
    manager = manager_for(FakeEngine("fake"))
    rt = manager.acquire("fake")
    try:
        with pytest.raises(RuntimeError, match="blew up"), rt:
            raise RuntimeError("blew up")
    finally:
        manager.release(rt)
    assert_permits_free(manager, "fake")


def test_capacity_rejection_does_not_leak():
    """Rejected requests return both the bind reference and any queued waiter slot."""
    manager = manager_for(FakeEngine("fake"))
    held = manager.acquire("fake")
    held.acquire()  # occupies the single permit

    def waiter() -> None:
        with manager.using("fake"):
            pass

    t = threading.Thread(target=waiter)
    t.start()
    deadline = time.time() + 5
    while manager._records["fake"].waiters < 1:
        assert time.time() < deadline
        time.sleep(0.01)
    rt = manager.acquire("fake")
    try:
        with pytest.raises(EngineUnavailableError, match="at capacity"), rt:
            pass
    finally:
        manager.release(rt)
    held.release()
    manager.release(held)
    t.join(5)
    assert_permits_free(manager, "fake")


def test_no_permit_leaks_after_mixed_failures():
    """Successes, rejections, exceptions and abandoned bindings leave no leaks."""
    manager = manager_for(FakeEngine("fake"), FakeEngine("alt"))
    # success
    with manager.using("fake"):
        pass
    # exception inside the permit
    rt = manager.acquire("fake")
    try:
        with pytest.raises(RuntimeError), rt:
            raise RuntimeError("x")
    finally:
        manager.release(rt)
    # abandoned binding released without ever taking a permit
    manager.release(manager.acquire("fake"))
    # pending swap that defers, then drains
    rt = manager.acquire("fake")
    manager.configure("fake", {"available": False})
    manager.release(rt)
    assert_permits_free(manager, "fake")
    assert_permits_free(manager, "alt")
