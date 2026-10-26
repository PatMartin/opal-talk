"""EngineRuntimeManager: instance ownership, serialized usage and lifecycle coordination."""

from __future__ import annotations

import threading
import time

import pytest

from conftest import FakeEngine
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
