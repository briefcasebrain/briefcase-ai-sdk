"""Red-team tests for the shared provider machinery: each one is a way the
first version changed the wrapped call, emitted a wrong record, or could
hang or crash the host program."""

import asyncio
import gc
import json
import logging
import sys
import threading

import pytest

import briefcase
from briefcase.integrations.providers import _base
from briefcase.integrations.providers._base import (
    SKIP,
    AsyncStreamProxy,
    PatchTarget,
    ProviderInstrumentor,
    StreamProxy,
    StreamState,
    atee,
    flush_abandoned,
    instrument_stream,
    jsonable,
    sync_generator_method,
    sync_method,
    tee,
    token_usage,
)

from tests.providers._helpers import drain_gc


@pytest.fixture
def inst(mem):
    instrumentor = ProviderInstrumentor("test", exporter=mem, async_capture=False)
    yield instrumentor
    instrumentor.uninstall()


# -- data correctness ---------------------------------------------------------

def test_a_missing_token_counter_is_none_never_zero():
    assert token_usage() is None
    assert token_usage(5, None) == {
        "prompt_tokens": 5, "completion_tokens": None, "total_tokens": None}
    assert token_usage(None, 7) == {
        "prompt_tokens": None, "completion_tokens": 7, "total_tokens": None}
    assert token_usage(5, 7)["total_tokens"] == 12
    assert token_usage(None, None, 9) == {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": 9}
    assert token_usage(True, "7", float("nan")) is None  # not numbers


def test_every_record_states_its_usage_even_when_unknown(inst, mem):
    inst.begin("llm.test", "f").finish({})
    assert mem.records[0]["token_usage"] is None


def test_jsonable_caps_the_total_size_and_survives_hostile_values(tmp_path):
    big = [{"role": "user", "content": "x" * 5000} for _ in range(200)]
    out = jsonable(big, limit=1000)
    assert len(json.dumps(out)) < 1000 * _base.TOTAL_CONTENT_FACTOR + 5000
    assert _base.TRUNCATED in out

    class Explodes:
        def model_dump(self, **kwargs):
            raise RuntimeError("boom")

        def __repr__(self):
            raise RuntimeError("never repr'd")

    cyclic = {}
    cyclic["self"] = cyclic
    with open(tmp_path / "f.bin", "wb") as handle:
        value = jsonable({
            "bytes": b"\x00\xff" * 10, "file": handle, "bad": Explodes(), "cycle": cyclic,
            "gen": (i for i in range(3)), 3: "non-string key", "set": {1, 2},
        })
    json.dumps(value)  # JSON-serialisable
    assert value["bytes"] == "<20 bytes>"
    assert value["file"] == "<BufferedWriter>" and value["bad"] == "<Explodes>"
    assert value["gen"] == "<generator>" and value["3"] == "non-string key"


def test_timestamps_are_timezone_aware(inst, mem):
    from datetime import datetime

    inst.begin("llm.test", "f").finish({})
    for key in ("started_at", "ended_at"):
        assert datetime.fromisoformat(mem.records[0][key]).tzinfo is not None


# -- statuses -----------------------------------------------------------------

@pytest.mark.parametrize("error,status", [
    (RuntimeError("x"), "error"),
    (asyncio.CancelledError(), "cancelled"),
    (KeyboardInterrupt(), "cancelled"),
    (SystemExit(3), "cancelled"),
])
def test_status_tells_an_sdk_failure_from_an_interruption(inst, mem, error, status):
    inst.begin("llm.test", "f").finish(error=error)
    record = mem.records[0]
    assert record["status"] == status
    assert record["error_type"] == type(error).__name__


def test_a_stream_that_was_not_read_to_its_end_is_incomplete_not_ok(inst, mem):
    call = inst.begin("llm.test", "f", stream=True)
    state = StreamState(call)
    stream = tee(iter([1, 2, 3]), state.on_item, state.on_end)
    next(stream)
    stream.close()
    assert mem.records[0]["status"] == "incomplete"
    assert mem.records[0]["outputs"] == {"chunk_count": 1, "stream_completed": False}


def test_a_stream_left_right_after_its_terminal_event_is_ok(inst, mem):
    class Finishes(StreamState):
        def observe(self, chunk):
            self.finished = chunk == "stop"

        def info(self):
            return {}

    call = inst.begin("llm.test", "f", stream=True)
    state = Finishes(call)
    stream = tee(iter(["a", "stop", "trailer"]), state.on_item, state.on_end)
    assert [next(stream), next(stream)] == ["a", "stop"]
    stream.close()
    assert mem.records[0]["status"] == "ok"
    assert mem.records[0]["outputs"]["stream_completed"] is False


# -- exceptions ---------------------------------------------------------------

@pytest.mark.parametrize("error", [
    ValueError("sdk"), KeyboardInterrupt(), SystemExit(2), asyncio.CancelledError(),
    GeneratorExit(),
])
def test_the_sdk_exception_propagates_as_the_same_object(inst, mem, error):
    def original():
        raise error

    wrapped = sync_method(original, lambda a, k: inst.begin("llm.test", "f"), lambda *a: None)
    with pytest.raises(BaseException) as caught:
        wrapped()
    assert caught.value is error
    assert len(mem.records) == 1
    # only the wrapper's frame was added in front of the SDK's
    frames = [f.name for f in __import__("traceback").extract_tb(caught.value.__traceback__)]
    assert frames[-2:] == ["wrapper", "original"]


def test_our_own_failures_never_reach_the_caller(mem):
    class BrokenExporter:
        def export(self, record):
            raise RuntimeError("exporter is down")

    inst = ProviderInstrumentor("test", exporter=BrokenExporter(), async_capture=False)
    try:
        def boom(*args):
            raise RuntimeError("bug in briefcase")

        # begin raises, end raises, the exporter raises: the call still works
        assert sync_method(lambda: "result", boom, boom)() == "result"
        wrapped = sync_method(lambda: "result", lambda a, k: inst.begin("llm.test", "f"), boom)
        assert wrapped() == "result"

        call = inst.begin("llm.test", "f", stream=True)
        chunks = list(tee(iter([1, 2]), boom, boom))
        assert chunks == [1, 2]
        call.finish({"usage": object(), "extra": "not a dict", "model": object()})
    finally:
        inst.uninstall()


def test_a_failing_async_end_still_returns_the_result(inst, mem):
    async def original():
        return "result"

    async def aend(call, result, args, kwargs):
        raise RuntimeError("bug in briefcase")

    wrapped = _base.async_method(
        original, lambda a, k: inst.begin("llm.test", "f"), lambda *a: None, aend)
    assert asyncio.run(wrapped()) == "result"
    assert len(mem.records) == 1 and mem.records[0]["capture_limits"]


# -- exactly once -------------------------------------------------------------

def test_finish_is_exactly_once_under_a_thread_race(inst, mem):
    for _ in range(200):
        call = inst.begin("llm.test", "f")
        barrier = threading.Barrier(8)

        def finish():
            barrier.wait()
            call.finish({})

        threads = [threading.Thread(target=finish) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert len(mem.records) == 200


def test_a_stream_consumed_twice_or_closed_twice_emits_one_record(inst, mem):
    call = inst.begin("llm.test", "f", stream=True)
    state = StreamState(call)
    stream = tee(iter([1, 2]), state.on_item, state.on_end)
    assert list(stream) == [1, 2] and list(stream) == []
    stream.close()
    state.on_end(False, None)
    assert len(mem.records) == 1 and mem.records[0]["status"] == "ok"


# -- abandonment and the garbage collector -------------------------------------

class _Stainless:
    """Shaped like openai.Stream: the object and its generator refer to each
    other, so only the cyclic collector frees an abandoned one."""

    def __init__(self, items):
        self._items = items
        self._iterator = self.__stream__()
        self.closed = 0

    def __stream__(self):
        yield from self._items

    def __iter__(self):
        for item in self._iterator:
            yield item

    def close(self):
        self.closed += 1


def test_an_abandoned_stream_keeps_its_chunk_count(inst, mem):
    call = inst.begin("llm.test", "f", stream=True)
    state = StreamState(call)
    stream = instrument_stream(_Stainless([1, 2, 3]), state.on_item, state.on_end, is_async=False)
    for seen, _ in enumerate(stream, 1):
        if seen == 2:
            break
    assert mem.records == []
    del stream, state, call
    drain_gc()
    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 2, "stream_completed": False}
    assert mem.records[0]["status"] == "incomplete"


def test_a_record_completed_inside_the_collector_is_not_exported_from_it(mem):
    """Exporting from a finalizer can re-enter a lock the interrupted thread
    holds (the export queue's mutex, an exporter's own lock) and hang the
    program. The export must happen later, on another thread."""
    exported_in_gc = []

    class Exporter:
        def export(self, record):
            exported_in_gc.append(_base.in_gc())

    inst = ProviderInstrumentor("test", exporter=Exporter(), async_capture=False)
    try:
        call = inst.begin("llm.test", "f", stream=True)
        state = StreamState(call)
        stream = instrument_stream(_Stainless([1]), state.on_item, state.on_end, is_async=False)
        del stream, state, call
        gc.collect()
        flush_abandoned()
        assert exported_in_gc == [False]
    finally:
        inst.uninstall()


def test_the_finalizer_thread_exports_without_being_asked(inst, mem):
    import time

    call = inst.begin("llm.test", "f", stream=True)
    state = StreamState(call)
    stream = instrument_stream(_Stainless([1]), state.on_item, state.on_end, is_async=False)
    del stream, state, call
    gc.collect()
    deadline = time.monotonic() + 5
    while not mem.records and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(mem.records) == 1


def test_an_async_stream_dropped_after_its_loop_closed_still_reports(inst, mem):
    async def source():
        for item in (1, 2, 3):
            yield item

    async def main():
        call = inst.begin("llm.test", "f", stream=True)
        state = StreamState(call)
        stream = atee(source(), state.on_item, state.on_end)
        await stream.__anext__()
        return stream

    loop = asyncio.new_event_loop()
    # Keep the loop from finalizing the generator: this is the case where
    # its ``finally`` never runs and only the __del__ backstop is left.
    loop.shutdown_asyncgens = lambda: asyncio.sleep(0)
    stream = loop.run_until_complete(main())
    hooks = sys.get_asyncgen_hooks()
    loop.close()
    sys.set_asyncgen_hooks(*hooks)
    del stream
    drain_gc()
    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 1, "stream_completed": False}


def test_nothing_is_kept_alive_after_the_record(inst, mem):
    import weakref

    class Big:
        pass

    payload = Big()
    ref = weakref.ref(payload)
    call = inst.begin("llm.test", "f", stream=True)
    state = StreamState(call)
    stream = tee(iter([payload]), state.on_item, state.on_end)
    del payload
    list(stream)
    del stream, state, call
    gc.collect()
    assert ref() is None


# -- transparency -------------------------------------------------------------

def test_tee_pulls_one_item_per_item_requested():
    pulled = []

    def source():
        for i in range(5):
            pulled.append(i)
            yield i

    stream = tee(source(), lambda item: None, lambda *a: None)
    next(stream)
    assert pulled == [0]
    next(stream)
    assert pulled == [0, 1]


def test_tee_can_hide_an_item_from_the_consumer():
    stream = tee(iter([1, 2, 3]), lambda item: SKIP if item == 2 else None, lambda *a: None)
    assert list(stream) == [1, 3]


def test_a_proxied_stream_still_passes_isinstance_and_delegates():
    class EventStream:
        marker = "inner"

        def __init__(self):
            self.closed = False

        def __iter__(self):
            return iter([1, 2])

        def close(self):
            self.closed = True

    inner = EventStream()
    ends = []
    proxy = StreamProxy(inner, lambda item: None, lambda *a: ends.append(a))
    assert isinstance(proxy, EventStream) and proxy.marker == "inner"
    assert list(proxy) == [1, 2]
    proxy.close()
    assert inner.closed and ends[0] == (True, None)

    class AsyncEventStream:
        def __aiter__(self):
            async def gen():
                yield 1
            return gen()

    async def main():
        proxy = AsyncStreamProxy(AsyncEventStream(), lambda item: None, lambda *a: None)
        assert isinstance(proxy, AsyncEventStream)
        return [item async for item in proxy]

    assert asyncio.run(main()) == [1]


def test_a_stream_that_cannot_be_instrumented_in_place_is_not_swapped_for_a_proxy():
    class NoIterator:
        def __iter__(self):
            return iter([1])

    stream = NoIterator()
    assert instrument_stream(stream, lambda i: None, lambda *a: None,
                             is_async=False, proxy=False) is None


def test_a_wrapped_generator_function_stays_one_and_stays_lazy(inst, mem):
    import inspect

    calls = []

    def original(*, model):
        """doc"""
        calls.append(model)
        yield 1
        yield 2

    wrapped = sync_generator_method(
        original, lambda a, k: inst.begin("llm.test", "f", stream=True),
        lambda call: StreamState(call))
    assert inspect.isgeneratorfunction(wrapped)
    assert inspect.signature(wrapped) == inspect.signature(original)
    assert wrapped.__wrapped__ is original and wrapped.__doc__ == "doc"
    generator = wrapped(model="m")
    del generator  # never iterated: the original never ran, so nothing happened
    drain_gc()
    assert calls == [] and mem.records == []
    assert list(wrapped(model="m")) == [1, 2]
    assert len(mem.records) == 1 and mem.records[0]["status"] == "ok"


# -- patch lifecycle ----------------------------------------------------------

class _SDK:
    class Resource:
        def create(self, *, model, stream=False):
            return f"sync:{model}"

        async def acreate(self, *, model, stream=False):
            return f"async:{model}"


def _factory(original):
    def wrapper(*args, **kwargs):
        return original(*args, **kwargs)
    return wrapper


@pytest.fixture
def sdk_module(monkeypatch):
    import types

    module = types.ModuleType("briefcase_fake_sdk")

    class Resource(_SDK.Resource):
        pass

    module.Resource = Resource
    monkeypatch.setitem(sys.modules, "briefcase_fake_sdk", module)
    return module


def test_a_group_is_patched_all_or_nothing(inst, sdk_module):
    create = sdk_module.Resource.__dict__.get("create", _SDK.Resource.create)
    ok = inst.patch_group("surface", [
        PatchTarget("briefcase_fake_sdk", "Resource", "create", _factory, ("model", "stream")),
        PatchTarget("briefcase_fake_sdk", "Resource", "renamed_away", _factory),
    ])
    assert ok is False and inst.patched == []
    assert sdk_module.Resource.create is create  # the first target was not left patched
    assert "renamed_away" in inst.skipped["surface"]


def test_a_changed_signature_is_treated_as_a_changed_sdk(inst, sdk_module):
    assert inst.patch_group("surface", [
        PatchTarget("briefcase_fake_sdk", "Resource", "create", _factory, ("model", "messages")),
    ]) is False
    assert "signature changed" in inst.skipped["surface"]
    assert "create" not in vars(sdk_module.Resource)


def test_skips_are_reported_in_one_warning(inst, sdk_module, caplog):
    inst.patch_group("a", [PatchTarget("briefcase_fake_sdk", "Resource", "gone", _factory)])
    inst.patch_group("b", [PatchTarget("briefcase_fake_sdk", "Nope", "create", _factory)])
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        inst.report_skipped()
        inst.report_skipped()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "NOT recorded" in warnings[0].getMessage() and "left untouched" in warnings[0].getMessage()


def test_undo_does_not_clobber_a_patch_applied_after_ours(inst, sdk_module, caplog):
    inst.patch("briefcase_fake_sdk", "Resource", "create", _factory, ("model",))
    ours = sdk_module.Resource.create
    theirs_calls = []

    def theirs(self, **kwargs):
        theirs_calls.append(kwargs)
        return ours(self, **kwargs)

    sdk_module.Resource.create = theirs
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        inst.uninstall()
    assert sdk_module.Resource.create is theirs
    assert "patched again by another library" in caplog.text
    assert sdk_module.Resource().create(model="m") == "sync:m" and theirs_calls


def test_a_patch_applied_before_ours_is_called_through(inst, sdk_module, mem):
    seen = []
    original = _SDK.Resource.create

    def theirs(self, **kwargs):
        seen.append("theirs")
        return original(self, **kwargs)

    sdk_module.Resource.create = theirs
    inst.patch(
        "briefcase_fake_sdk", "Resource", "create",
        lambda fn: sync_method(fn, lambda a, k: inst.begin("llm.test", "f"),
                               lambda call, result, a, k: (call.finish({}), result)[1]),
    )
    assert sdk_module.Resource().create(model="m") == "sync:m"
    assert seen == ["theirs"] and len(mem.records) == 1
    inst.uninstall()
    assert sdk_module.Resource.create is theirs


def test_a_wrapper_captured_before_undo_goes_quiet_after_it(inst, sdk_module, mem):
    inst.patch(
        "briefcase_fake_sdk", "Resource", "create",
        lambda fn: sync_method(fn, lambda a, k: inst.begin("llm.test", "f"),
                               lambda call, result, a, k: (call.finish({}), result)[1]),
    )
    bound = sdk_module.Resource().create  # what with_raw_response keeps
    inst.uninstall()
    assert bound(model="m") == "sync:m"
    assert mem.records == []


def test_the_gc_callback_is_removed_with_the_last_instrumentor(mem):
    before = _base._gc_users
    a = ProviderInstrumentor("a", exporter=mem)
    b = ProviderInstrumentor("b", exporter=mem)
    assert _base._gc_callback in gc.callbacks
    a.uninstall()
    assert _base._gc_callback in gc.callbacks
    b.uninstall()
    assert (_base._gc_callback in gc.callbacks) == (before > 0)


def test_capture_content_off_keeps_error_text_out(mem):
    inst = ProviderInstrumentor("test", exporter=mem, async_capture=False)
    try:
        inst.begin("llm.test", "f", inputs=lambda: {"messages": "SECRET"}).finish(
            {"content": {"content": "SECRET"}}, error=RuntimeError("SECRET in the message"))
        assert "SECRET" not in json.dumps(mem.records[0])
    finally:
        inst.uninstall()


def test_many_threads_and_tasks_do_not_mix_up_records(mem):
    inst = ProviderInstrumentor("test", exporter=mem, async_capture=False)
    try:
        def original(*, model):
            return model

        wrapped = sync_method(
            original, lambda a, k: inst.begin("llm.test", "f", model=k["model"]),
            lambda call, result, a, k: (call.finish({"response_id": result}), result)[1])

        def work(i):
            for j in range(50):
                assert wrapped(model=f"m{i}-{j}") == f"m{i}-{j}"

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        async def task(i):
            for j in range(20):
                await asyncio.sleep(0)
                wrapped(model=f"t{i}-{j}")

        async def main():
            await asyncio.gather(*[task(i) for i in range(10)])

        asyncio.run(main())
        assert len(mem.records) == 8 * 50 + 10 * 20
        assert all(r["model_parameters"]["model_name"] == r["response_id"] for r in mem.records)
        assert len({r["decision_id"] for r in mem.records}) == len(mem.records)
    finally:
        inst.uninstall()


def test_a_full_export_queue_drops_records_and_never_blocks_the_call(mem):
    import time

    from briefcase import _export_mixin

    release = threading.Event()

    class Stuck:
        def export(self, record):
            release.wait(10)

    inst = ProviderInstrumentor("test", exporter=Stuck(), async_capture=True)
    stats = briefcase.export_stats()
    briefcase.configure_export(max_queue_size=5)
    try:
        started = time.monotonic()
        for _ in range(200):
            inst.begin("llm.test", "f").finish({})
        assert time.monotonic() - started < 2.0
        assert briefcase.export_stats()["dropped"] - stats["dropped"] >= 190
    finally:
        release.set()
        _export_mixin.wait_for_pending_exports(5)
        briefcase.configure_export(max_queue_size=stats["max_queue_size"])
        inst.uninstall()
