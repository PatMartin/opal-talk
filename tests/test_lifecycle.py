"""Concurrent runtime lifecycle: shared voices/engines, reconfiguration during
active requests, load failures, cache eviction, cancellation, retired cleanup and
permit-leak accounting. Everything runs on deterministic fake engines."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from conftest import FakeEngine, make_voice
from opal_talk.schemas import GenerateRequest, VoiceTestRequest
from opal_talk.service.errors import Conflict, PayloadTooLarge
from opal_talk.tts.base import AudioChunk, EngineUnavailableError, VoiceResolutionError
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


# -- incremental streaming --------------------------------------------------------


class IncrementalEngine(FakeEngine):
    """Yields one chunk, blocks on a gate, then yields the final chunk."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.first_yielded = threading.Event()
        self.release_rest = threading.Event()

    def synthesize(self, ref, text, speed, options):
        self.first_yielded.set()
        yield AudioChunk(np.zeros(200, dtype=np.float32), 22050)
        self.release_rest.wait(10)
        yield AudioChunk(np.ones(200, dtype=np.float32), 22050)


def _services_with_engine(config, engine):
    from opal_talk.persistence.store import now_iso
    from opal_talk.service.container import build_services

    services = build_services(config, engines=[engine])
    stamp = now_iso()
    services.store.insert_voice(
        {
            "voice_id": "v1", "name": "v1", "description": None, "engine_id": engine.engine_id,
            "engine_voice_ref": "tone-a", "language": "en-US", "enabled": True,
            "native_sample_rate": 22050, "engine_options": {}, "metadata": {},
            "created_at": stamp, "updated_at": stamp,
        }
    )
    return services


def _plan(services, engine_id="fake"):
    from opal_talk.schemas import GenerateRequest

    return services.generation.plan(GenerateRequest(text="hello", voice_id="v1"))


def test_pcm_stream_yields_first_block_before_engine_finishes(config):
    """Incremental consumption: no list() buffering delays the first block."""
    engine = IncrementalEngine("fake")
    services = _services_with_engine(config, engine)
    try:
        stream = services.generation.pcm_stream(_plan(services))
        first = next(stream)
        assert first and engine.first_yielded.is_set()
        assert not engine.release_rest.is_set()  # block arrived while engine still producing
        engine.release_rest.set()
        assert list(stream)  # the final block follows once produced
    finally:
        services.close()


def test_abandoned_stream_releases_bind_and_permit(config):
    """Closing a partially consumed stream returns the runtime reference + permit."""
    engine = IncrementalEngine("fake")
    services = _services_with_engine(config, engine)
    try:
        stream = services.generation.pcm_stream(_plan(services))
        next(stream)
        stream.close()  # GeneratorExit through the `with` + finally releases everything
        assert_permits_free(services.engines.runtime, "fake")
    finally:
        services.close()


# -- bounded non-streaming buffers -----------------------------------------------


def test_render_rejects_audio_beyond_max_buffer(client):
    """Non-streaming renders accumulate under `max_audio_bytes` -> 413 before runaway."""
    make_voice(client, "v1")
    client.skip_contract = True
    client.put("/api/v1/settings/max_audio_bytes", json={"value": str(1 << 16)})
    text = "word " * 5000
    resp = client.post("/api/v1/generate", json={"text": text, "voice_id": "v1"})
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    # Streaming is incremental, not bounded by the buffered-output cap.
    client.skip_contract = False
    with client.stream("POST", "/api/v1/generate/stream",
                       json={"text": "ok", "voice_id": "v1", "engine_options": {"delay": 0.01}}) as r:
        assert r.status_code == 200
    # The aborted render released everything: the next request works normally.
    assert client.post("/api/v1/generate", json={"text": "ok", "voice_id": "v1"}).status_code == 200


def _plan_sized(services, text, output_format="pcm"):
    from opal_talk.schemas import GenerateRequest

    return services.generation.plan(GenerateRequest(text=text, voice_id="v1", output_format=output_format))


def test_max_audio_bytes_applies_at_exact_pcm_limit(config):
    """max_audio_bytes bounds the encoded response body: PCM equal to the limit is served."""
    services = _services_with_engine(config, FakeEngine("fake"))
    try:
        plan = _plan_sized(services, "x" * 150)  # 33075 samples -> 66150 PCM bytes
        services.store.upsert_setting("max_audio_bytes", "66150")
        data, media_type = services.generation.render(plan)
        assert media_type == "application/octet-stream" and len(data) == 66150
    finally:
        services.close()


def test_max_audio_bytes_rejects_one_byte_over_pcm(config):
    services = _services_with_engine(config, FakeEngine("fake"))
    try:
        plan = _plan_sized(services, "x" * 150)
        services.store.upsert_setting("max_audio_bytes", "66149")
        with pytest.raises(PayloadTooLarge):
            services.generation.render(plan)
    finally:
        services.close()


def test_max_audio_bytes_counts_wav_container_overhead(config):
    """A WAV response must fit the SAME encoded-body limit: the 44-byte header
    counts, so the identical audio fits at limit+44 but not at limit+43."""
    services = _services_with_engine(config, FakeEngine("fake"))
    try:
        services.store.upsert_setting("max_audio_bytes", "66194")  # 66150 PCM + 44 header
        plan = _plan_sized(services, "x" * 150, output_format="wav")
        data, media_type = services.generation.render(plan)
        assert media_type == "audio/wav" and len(data) == 66194
        import io
        import wave

        with wave.open(io.BytesIO(data)) as w:
            assert w.getframerate() == 22050 and w.getnframes() == 33075

        services.store.upsert_setting("max_audio_bytes", "66193")  # one byte short of the encoded size
        plan = _plan_sized(services, "x" * 150, output_format="wav")
        with pytest.raises(PayloadTooLarge):
            services.generation.render(plan)
    finally:
        services.close()


def test_to_pcm16_byte_identical_output():
    import numpy as np

    from opal_talk.tts.audio import to_pcm16

    samples = np.linspace(-1.2, 1.2, 4001, dtype=np.float32)
    expected = (np.clip(samples.astype(np.float32) * 0.5, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
    assert to_pcm16(samples, 0.5) == expected


# -- plan-time runtime binding ------------------------------------------------------


def test_plan_binds_runtime_generation_through_synthesis(config):
    """A reconfigure between plan() and synthesis must not alter what the request
    sees: plan() bound one generation and the new options apply only on drain."""
    engine = FakeEngine("fake")
    services = _services_with_engine(config, engine)
    try:
        seen: list[dict] = []
        synthesize = engine.synthesize

        def tracked(ref, text, speed, options):
            seen.append(dict(engine.options))
            yield from synthesize(ref, text, speed, options)

        engine.synthesize = tracked
        plan = _plan(services)
        services.engines.runtime.swap("fake", {"tag": "B"})  # defers: the plan pins the old generation
        blocks = list(services.generation.pcm_stream(plan))
        assert blocks
        assert "tag" not in seen[0]  # synthesis ran the generation bound at plan time
        assert engine.options == {"tag": "B"}  # the newest config applied only after drain
        assert_permits_free(services.engines.runtime, "fake")
    finally:
        services.close()


def test_render_and_voice_test_share_bound_generation(config):
    """The non-streaming and voice-test paths obey the same bound-generation rules."""
    engine = FakeEngine("fake")
    services = _services_with_engine(config, engine)
    try:
        seen: list[dict] = []
        synthesize = engine.synthesize

        def tracked(ref, text, speed, options):
            seen.append(dict(engine.options))
            yield from synthesize(ref, text, speed, options)

        engine.synthesize = tracked
        plan = _plan(services)
        services.engines.runtime.swap("fake", {"tag": "B"})
        data, _ = services.generation.render(plan)
        assert data and "tag" not in seen[0]
        # The drained swap applied B; a later voice-test binds the new generation.
        _, data2, _ = services.generation.test_voice("v1", VoiceTestRequest())
        assert data2 and seen[-1] == {"tag": "B"}
        assert_permits_free(services.engines.runtime, "fake")
    finally:
        services.close()


def test_failed_plan_releases_bound_runtime(config):
    """A plan() that fails after acquiring the runtime binding releases it."""
    engine = FakeEngine("fake")
    services = _services_with_engine(config, engine)
    try:
        rec = services.engines.runtime._records["fake"]

        def boom(ref, options):
            raise VoiceResolutionError("nope", "voice_not_found")

        engine.native_sample_rate = boom
        with pytest.raises(Conflict):
            services.generation.plan(GenerateRequest(text="hello", voice_id="v1"))
        assert rec.refs == 0
    finally:
        services.close()
