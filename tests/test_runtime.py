"""EngineRuntimeManager: instance ownership, serialized usage and lifecycle coordination."""

from __future__ import annotations

import threading
import time

import pytest

from conftest import FakeEngine
from opal_talk.tts.base import EngineUnavailableError
from opal_talk.tts.registry import EngineRegistry
from opal_talk.tts.runtime import EngineRuntime, EngineRuntimeManager


@pytest.fixture
def manager() -> EngineRuntimeManager:
    return EngineRuntimeManager(EngineRegistry([FakeEngine("fake"), FakeEngine("alt")]))


def test_lookup_passthrough(manager):
    assert manager.ids() == ["alt", "fake"]
    assert [e.engine_id for e in manager.all()] == ["alt", "fake"]
    assert manager.get("fake").engine_id == "fake"
    assert manager.get("ghost") is None


def test_acquire_and_release(manager):
    rt = manager.acquire("fake")
    assert isinstance(rt, EngineRuntime)
    assert rt.engine is manager.get("fake")
    manager.release(rt)
    with manager.acquire("fake") as rt2:  # lock was freed: reacquire does not block
        assert rt2.engine.engine_id == "fake"


def test_acquire_unknown_engine(manager):
    with pytest.raises(KeyError, match="ghost"):
        manager.acquire("ghost")


def test_using_releases_on_exit_and_on_error(manager):
    with manager.using("fake") as engine:
        assert engine.engine_id == "fake"
    with manager.using("fake"):
        pass
    with pytest.raises(RuntimeError, match="boom"), manager.using("fake"):
        raise RuntimeError("boom")
    manager.acquire("fake")  # would deadlock if the error path left the lock held


def test_same_engine_usage_is_serialized(manager):
    """A second usage reference on one engine blocks until the first releases."""
    gate = threading.Event()
    order: list[str] = []

    def first() -> None:
        with manager.using("fake"):
            order.append("first-in")
            gate.wait(5)
            order.append("first-out")

    def second() -> None:
        with manager.using("fake"):
            order.append("second")

    t1 = threading.Thread(target=first)
    t1.start()
    deadline = time.time() + 5
    while "first-in" not in order:
        assert time.time() < deadline
        time.sleep(0.01)
    t2 = threading.Thread(target=second)
    t2.start()
    time.sleep(0.15)
    assert "second" not in order  # still blocked on the same engine
    gate.set()
    t1.join(5)
    t2.join(5)
    assert order == ["first-in", "first-out", "second"]


def test_different_engines_serialize_independently(manager):
    """The per-engine locks mirror the behavior GenerationService had before."""
    entered = threading.Event()

    def holds_fake() -> None:
        with manager.using("fake"):
            entered.set()
            time.sleep(0.15)

    t = threading.Thread(target=holds_fake)
    t.start()
    assert entered.wait(5)
    with manager.using("alt"):  # another engine is unaffected
        pass
    t.join(5)


def test_configure_pushes_options_into_the_managed_instance(manager):
    manager.configure("fake", {"available": False})
    engine = manager.get("fake")
    assert engine.options["available"] is False
    assert engine.availability() == (False, "fake runtime disabled by options")
    manager.configure("ghost", {"available": True})  # unknown id: harmless no-op


def test_container_wires_the_manager(app):
    runtime = app.state.services.runtime
    assert isinstance(runtime, EngineRuntimeManager)
    assert runtime is app.state.services.engines.runtime
    assert runtime.get("fake") is app.state.services.engines.runtime.get("fake")


class ConfigBoomEngine(FakeEngine):
    """configure() raises when options carry the marker key."""

    def configure(self, options):
        if options.get("boom"):
            raise EngineUnavailableError("configure blew up")
        super().configure(options)


def test_reconfigure_defers_until_active_request_finishes(manager):
    rt = manager.acquire("fake")
    manager.configure("fake", {"available": False})
    engine = manager.get("fake")
    # The bound request still sees the old configuration generation.
    assert engine.options.get("available", True) is True
    manager.release(rt)
    assert engine.options["available"] is False  # applied on drain


def test_new_acquire_waits_for_pending_reconfigure(manager):
    rt = manager.acquire("fake")
    manager.configure("fake", {"available": False})
    seen: list[bool] = []

    def wait_acquire() -> None:
        with manager.using("fake") as engine:
            seen.append(engine.options["available"])

    t = threading.Thread(target=wait_acquire)
    t.start()
    time.sleep(0.15)
    assert seen == []  # blocked until the pending generation is applied
    manager.release(rt)
    t.join(5)
    assert seen == [False]


def test_failed_swap_leaves_previous_record_active():
    manager = EngineRuntimeManager(EngineRegistry([ConfigBoomEngine("fake")]))
    with pytest.raises(EngineUnavailableError, match="blew up"):
        manager.swap("fake", {"boom": True})
    with manager.using("fake") as engine:  # old generation still serves
        assert engine.options.get("boom") is None


def test_failed_deferred_apply_surfaces_to_new_requests():
    manager = EngineRuntimeManager(EngineRegistry([ConfigBoomEngine("fake")]))
    rt = manager.acquire("fake")
    manager.configure("fake", {"boom": True})  # deferred: applies on drain, fails
    manager.release(rt)
    with pytest.raises(EngineUnavailableError, match="reconfiguration failed"):
        manager.acquire("fake")


def test_synthesis_uninterrupted_by_reconfigure(manager):
    engine = manager.get("fake")
    done = threading.Event()

    def generate() -> None:
        rt = manager.acquire("fake")
        try:
            with rt:
                list(rt.engine.synthesize("tone-a", "x", 1.0, {"delay": 0.2}))
        finally:
            manager.release(rt)
        done.set()

    t = threading.Thread(target=generate)
    t.start()
    time.sleep(0.05)
    manager.configure("fake", {"available": False})  # lands mid-synthesis
    assert done.wait(5)
    t.join(5)
    assert len(engine.calls) == 1  # request completed on its bound generation
    assert engine.options["available"] is False  # new options applied only after drain


def test_update_rolls_back_runtime_when_store_write_fails(config, monkeypatch):
    from opal_talk.service.container import build_services

    services = build_services(config, engines=[FakeEngine("fake")])
    engine = services.runtime.get("fake")
    old_options = services.store.get_engine_state("fake")["options"]

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(services.store, "upsert_engine_state", boom)
    with pytest.raises(RuntimeError, match="db down"):
        services.engines.update("fake", {"options": {"available": False}})
    # DB and active runtime stay consistent: both still carry the old options.
    assert services.store.get_engine_state("fake")["options"] == old_options
    assert engine.options.get("available", True) is True
    services.close()
