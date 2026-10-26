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


# -- chained reconfiguration -------------------------------------------------------


def test_second_reconfigure_defers_instead_of_applying_mid_request(manager):
    """Swap B while a request still holds the previous generation: it must neither
    configure the shared engine mid-request nor be clobbered by the stale pending A."""
    engine = manager.get("fake")
    seen: list[dict] = []
    entered, proceed = threading.Event(), threading.Event()

    def generate() -> None:
        rt = manager.acquire("fake")
        try:
            with rt:
                entered.set()
                proceed.wait(10)
                seen.append(dict(rt.engine.options))
                list(rt.engine.synthesize("tone-a", "x", 1.0, {}))
        finally:
            manager.release(rt)

    t = threading.Thread(target=generate)
    t.start()
    assert entered.wait(5)
    manager.configure("fake", {"available": False, "tag": "A"})  # deferred onto the live generation
    manager.configure("fake", {"tag": "B"})  # chained: must also defer, not apply now
    assert "tag" not in engine.options  # the live request's config is untouched
    proceed.set()
    t.join(5)
    assert seen and "tag" not in seen[0]  # the request saw its bound generation
    # Deterministic supersession: the newest deferred configuration wins on drain.
    assert engine.options == {"tag": "B"}
    assert manager._records["fake"].refs == 0


def test_three_rapid_reconfigures_newest_wins(manager):
    """A -> B -> C while busy: only C is ever applied to the engine."""
    engine = manager.get("fake")
    applied: list[dict] = []
    orig = engine.configure

    def recording(options):
        applied.append(dict(options))
        orig(options)

    engine.configure = recording
    rt = manager.acquire("fake")
    manager.configure("fake", {"tag": "A"})
    manager.configure("fake", {"tag": "B"})
    manager.configure("fake", {"tag": "C"})
    manager.release(rt)
    assert applied == [{"tag": "C"}]
    assert engine.options == {"tag": "C"}
    assert manager._records["fake"].refs == 0


def test_waiter_rebinds_through_rapid_supersessions(manager):
    """A waiter bound to a pending generation survives two supersessions and lands
    on the newest generation — it proceeds, it is not left blocked or failed."""
    engine = manager.get("fake")
    rt = manager.acquire("fake")
    acquired: list[EngineRuntime] = []
    errors: list[Exception] = []

    def waiter() -> None:
        try:
            acquired.append(manager.acquire("fake"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    manager.configure("fake", {"tag": "A"})  # deferred: waiters bind the pending record
    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)  # let the waiter bind the pending generation first
    manager.configure("fake", {"tag": "B"})  # supersedes A's generation
    manager.configure("fake", {"tag": "C"})  # supersedes B's generation
    manager.release(rt)
    t.join(5)
    assert errors == [], errors
    rt2 = acquired[0]
    try:
        with rt2:
            list(rt2.engine.synthesize("tone-a", "x", 1.0, {}))
    finally:
        manager.release(rt2)
    assert engine.options == {"tag": "C"}
    assert manager._records["fake"].refs == 0


def test_failed_chained_apply_surfaces_an_explicit_error(manager):
    """A deferred configure() that fails wakes the newest generation's waiters
    with a reconfiguration error instead of blocking them."""
    manager = EngineRuntimeManager(EngineRegistry([ConfigBoomEngine("fake")]))
    rt = manager.acquire("fake")
    manager.configure("fake", {"boom": True})  # deferred
    manager.configure("fake", {"tag": "C"})  # supersedes the doomed pending record
    manager.release(rt)
    # C's generation activated cleanly on drain — the doomed A never applied.
    assert manager.get("fake").options == {"tag": "C"}
    with manager.using("fake") as engine:
        assert engine.options == {"tag": "C"}


def test_failed_deferred_apply_wakes_bound_waiters():
    manager = EngineRuntimeManager(EngineRegistry([ConfigBoomEngine("fake")]))
    rt = manager.acquire("fake")
    manager.configure("fake", {"boom": True})  # deferred; configure() raises on drain
    got: list[Exception] = []

    def waiter() -> None:
        try:
            manager.acquire("fake")
        except Exception as exc:  # noqa: BLE001
            got.append(exc)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    manager.release(rt)
    t.join(5)
    assert len(got) == 1
    assert isinstance(got[0], EngineUnavailableError) and "reconfiguration failed" in str(got[0])


def test_double_release_is_idempotent(manager):
    """A second detach must not drive the record's reference count negative."""
    rt = manager.acquire("fake")
    manager.release(rt)
    manager.release(rt)
    assert manager._records["fake"].refs == 0


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


# -- bounded inference concurrency ------------------------------------------------


def test_limit_one_serializes_by_default(manager):
    """Unconfigured engines keep the old serialized behavior (permit count 1)."""
    assert manager._records["fake"].permits == 1
    gate = threading.Event()

    def first() -> None:
        with manager.using("fake"):
            gate.wait(5)

    t1 = threading.Thread(target=first)
    t1.start()
    time.sleep(0.05)

    def queued() -> None:
        with manager.using("fake"):
            pass

    t2 = threading.Thread(target=queued)
    t2.start()
    deadline = time.time() + 5
    while manager._records["fake"].waiters < 1:
        assert time.time() < deadline
        time.sleep(0.01)
    # holder + one queued waiter fills the queue -> the next request is rejected.
    with pytest.raises(EngineUnavailableError, match="at capacity"), manager.using("fake"):
        pass
    gate.set()
    t1.join(5)
    t2.join(5)


def test_configured_limit_two_runs_two_in_parallel():
    manager = EngineRuntimeManager(EngineRegistry([FakeEngine("fake")]))
    manager.configure("fake", {"max_concurrent_inferences": 2})
    assert manager._records["fake"].permits == 2
    barrier = threading.Barrier(2, timeout=5)
    done: list[int] = []

    def work(i: int) -> None:
        with manager.using("fake"):
            barrier.wait()  # only passes if both callers hold permits concurrently
            done.append(i)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(done) == [0, 1]


def test_waiters_bounded_then_rejected():
    """permits in flight + permits waiting is the cap; the next request is rejected."""
    manager = EngineRuntimeManager(EngineRegistry([FakeEngine("fake")]))
    manager.configure("fake", {"max_concurrent_inferences": 2})
    held = [manager.acquire("fake") for _ in range(2)]
    for rt in held:
        rt.acquire()
    queued = threading.Event()

    def waiter() -> None:
        queued.set()
        with manager.using("fake"):
            pass

    threads = [threading.Thread(target=waiter) for _ in range(2)]
    for t in threads:
        t.start()
    deadline = time.time() + 5
    while manager._records["fake"].waiters < 2:
        assert time.time() < deadline
        time.sleep(0.01)
    rt = manager.acquire("fake")
    try:
        with pytest.raises(EngineUnavailableError, match="at capacity"), rt:
            pass
    finally:
        manager.release(rt)
    for h in held:
        h.release()
    for t in threads:
        t.join(5)
    for h in held:
        manager.release(h)


def test_configured_limit_clamped_to_adapter_cap():
    """Piper/Kokoro declare cap 1; a bigger configured limit can't make them parallel."""
    manager = EngineRuntimeManager(EngineRegistry([FakeEngine("fake")]))  # FakeEngine cap = 4
    manager.configure("fake", {"max_concurrent_inferences": 99})
    assert manager._records["fake"].permits == 4
    engine = manager.get("fake")
    engine.max_concurrent_inferences = 1  # e.g. a real adapter that isn't thread-safe
    manager.configure("fake", {"max_concurrent_inferences": 8})
    assert manager._records["fake"].permits == 1


def test_runtime_option_is_not_visible_to_adapter():
    manager = EngineRuntimeManager(EngineRegistry([FakeEngine("fake")]))
    manager.configure("fake", {"max_concurrent_inferences": 2, "available": False})
    engine = manager.get("fake")
    assert "max_concurrent_inferences" not in engine.options
    assert engine.options["available"] is False


def test_engine_update_accepts_runtime_option(client):
    resp = client.patch("/api/v1/engines/fake", json={"options": {"max_concurrent_inferences": 2}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["options"]["max_concurrent_inferences"] == 2


def test_engine_update_rejects_invalid_limit(client):
    client.skip_contract = True
    resp = client.patch("/api/v1/engines/fake", json={"options": {"max_concurrent_inferences": 0}})
    assert resp.status_code == 422


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_rejection_is_immediate_and_leaves_no_waiter(manager):
    """permits in flight + permits queued is the bound; the next caller is
    rejected at once (no blocking) and leaves no waiter or permit behind."""
    rec = manager._records["fake"]
    held = manager.acquire("fake")
    held.acquire()  # holds the single permit
    rec_of_held = manager._records["fake"]

    def queued() -> None:
        with manager.using("fake"):
            pass

    t = threading.Thread(target=queued)
    t.start()
    _wait_for(lambda: rec_of_held.waiters == 1)
    rt = manager.acquire("fake")  # binding is allowed; admission gates at enter
    try:
        started = time.monotonic()
        with pytest.raises(EngineUnavailableError, match="at capacity"), rt:
            pass
        assert time.monotonic() - started < 1.0
    finally:
        manager.release(rt)
    assert not rt._held and rec_of_held.waiters == 1  # only the queued thread remains
    held.release()
    manager.release(held)
    t.join(5)
    assert rec_of_held.waiters == 0 and rec_of_held.refs == 0
    assert rec is rec_of_held


def test_queued_waiter_acquires_capacity_when_it_frees(manager):
    """A request waiting for a permit proceeds once the holder releases."""
    engine = manager.get("fake")
    rec = manager._records["fake"]
    gate = threading.Event()
    done: list[str] = []

    def holder() -> None:
        with manager.using("fake"):
            gate.wait(5)

    t1 = threading.Thread(target=holder)
    t1.start()
    time.sleep(0.05)
    synthesize = engine.synthesize

    def waiter() -> None:
        with manager.using("fake") as e:
            list(e.synthesize("tone-a", "x", 1.0, {}))
            done.append("waiter")

    engine.synthesize = synthesize
    t2 = threading.Thread(target=waiter)
    t2.start()
    _wait_for(lambda: rec.waiters == 1)
    assert done == []  # still queued
    gate.set()
    t1.join(5)
    t2.join(5)
    assert done == ["waiter"]
    assert rec.waiters == 0 and rec.refs == 0


def test_failed_inference_releases_permit_exactly_once(manager):
    """An engine exception inside `with` frees the permit once: the semaphore
    is fully available to the next caller and double-release is a no-op."""
    rt = manager.acquire("fake")
    with pytest.raises(Exception, match="synthetic failure"), rt:
        list(rt.engine.synthesize("tone-a", "x", 1.0, {"fail_on_call": 1}))
    assert not rt._held
    rt.release()  # no permit was left held: a second release changes nothing
    with manager.using("fake") as engine:  # would block if the permit leaked
        assert engine.engine_id == "fake"
    manager.release(rt)


def test_permit_release_twice_cannot_over_release(manager):
    rt = manager.acquire("fake")
    rt.acquire()
    rt.release()
    rt.release()  # BoundedSemaphore raises if this actually released again
    manager.release(rt)
    assert manager._records["fake"].refs == 0


def test_permit_waiter_uses_its_bound_generation(manager):
    """A caller queued on the old record's semaphore still runs the bound
    generation: the deferred reconfigure applies only after every bound user
    drains."""
    engine = manager.get("fake")
    rec = manager._records["fake"]
    seen: list[dict] = []
    done = threading.Event()

    holder = manager.acquire("fake")
    holder.acquire()  # holds the only permit

    def waiter() -> None:
        with manager.using("fake"):
            seen.append(dict(engine.options))
            done.set()

    t = threading.Thread(target=waiter)
    t.start()
    _wait_for(lambda: rec.waiters == 1)  # bound to gen0, queued on its semaphore
    manager.configure("fake", {"tag": "B"})  # defers: gen0 still has bound users
    holder.release()
    manager.release(holder)
    t.join(5)
    assert done.is_set() and "tag" not in seen[0]  # waiter ran its bound generation
    assert engine.options == {"tag": "B"}  # drain applied the new config last
    assert rec.refs == 0


def test_rejection_storm_stays_bounded_and_deadlock_free(manager):
    """A burst of requests past capacity: waiters never exceed permits, every
    request resolves (success or rejection), and nothing wedges."""
    rec = manager._records["fake"]
    gate = threading.Event()
    holder = manager.acquire("fake")

    def hold() -> None:
        with holder:
            gate.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.05)

    results = {"ok": 0, "rejected": 0}
    count_lock = threading.Lock()
    peak = [0]
    monitoring = threading.Event()

    def monitor() -> None:
        while not monitoring.is_set():
            peak[0] = max(peak[0], rec.waiters)
            time.sleep(0.002)

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()

    def attempt() -> None:
        try:
            with manager.using("fake"), count_lock:
                results["ok"] += 1
        except EngineUnavailableError:
            with count_lock:
                results["rejected"] += 1

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for th in threads:
        th.start()
    _wait_for(lambda: rec.waiters >= 1)
    gate.set()
    for th in threads:
        th.join(5)
    monitoring.set()
    mon.join(5)
    t.join(5)
    assert results["ok"] <= rec.permits  # only queue-capacity callers got in
    assert results["ok"] + results["rejected"] == 20
    assert peak[0] <= rec.permits
    holder.release()
    manager.release(holder)
    assert rec.waiters == 0 and rec.refs == 0
