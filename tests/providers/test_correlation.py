"""Framework + provider double recording: the two records of one model call
are linked, and ``dedupe`` can drop one side."""

import asyncio
import contextvars
import threading

import pytest

from briefcase.integrations.providers import _correlation
from briefcase.integrations.providers._base import ProviderInstrumentor
from briefcase.integrations.providers._correlation import (
    current_llm_run,
    llm_run_finished,
    llm_run_started,
    set_dedupe,
)


@pytest.fixture(autouse=True)
def _reset_dedupe():
    from briefcase.integrations.providers._correlation import reset_context

    set_dedupe("none")
    reset_context()
    yield
    set_dedupe("none")
    reset_context()


@pytest.fixture
def inst(mem):
    instrumentor = ProviderInstrumentor("openai", exporter=mem, async_capture=False)
    yield instrumentor
    instrumentor.uninstall()


def _provider_call(inst):
    call = inst.begin("llm.openai.chat", "openai.chat.completions.create")
    if call is not None:
        call.finish({})
    return call


def test_a_provider_record_carries_the_active_framework_run(inst, mem):
    _provider_call(inst)
    scope = llm_run_started("langchain", "run-1")
    _provider_call(inst)
    assert llm_run_finished(scope) is False  # dedupe="none": keep both
    _provider_call(inst)
    assert [(r.get("parent_run_id"), r.get("parent_framework")) for r in mem.records] == [
        (None, None), ("run-1", "langchain"), (None, None)]
    assert scope.provider_records == 1


def test_the_run_is_seen_by_its_own_thread_only(inst, mem):
    started, release = threading.Event(), threading.Event()

    def framework_thread():
        scope = llm_run_started("langchain", "thread-run")
        started.set()
        release.wait(5)
        _provider_call(inst)
        llm_run_finished(scope)

    thread = threading.Thread(target=framework_thread)
    thread.start()
    started.wait(5)
    assert current_llm_run() is None
    _provider_call(inst)  # another thread's call must not be attributed
    release.set()
    thread.join()
    assert [r.get("parent_run_id") for r in mem.records] == [None, "thread-run"]


def test_concurrent_tasks_do_not_leak_runs_into_each_other(inst, mem):
    async def framework_task(i):
        scope = llm_run_started("llamaindex", f"run-{i}")
        await asyncio.sleep(0.001 * (5 - i))
        call = inst.begin("llm.openai.chat", "f", model=f"m{i}")
        await asyncio.sleep(0)
        call.finish({})
        llm_run_finished(scope)

    async def bystander():
        await asyncio.sleep(0.002)
        assert current_llm_run() is None
        inst.begin("llm.openai.chat", "f", model="bystander").finish({})

    async def main():
        await asyncio.gather(*[framework_task(i) for i in range(5)], bystander())
        assert current_llm_run() is None

    asyncio.run(main())
    by_model = {r["model_parameters"]["model_name"]: r.get("parent_run_id") for r in mem.records}
    assert by_model == {**{f"m{i}": f"run-{i}" for i in range(5)}, "bystander": None}


def test_a_child_task_and_a_copied_context_inherit_the_run(inst, mem):
    async def main():
        scope = llm_run_started("langchain", "parent")
        await asyncio.ensure_future(asyncio.to_thread(_provider_call, inst))
        contextvars.copy_context().run(_provider_call, inst)
        llm_run_finished(scope)

    asyncio.run(main())
    assert [r.get("parent_run_id") for r in mem.records] == ["parent", "parent"]


def test_a_run_ended_from_another_context_cannot_be_attributed_later(inst, mem):
    """Frameworks may report the end from another thread. The variable in the
    starting context cannot be reset from there; the scope is switched off
    instead, so a later call in that context is not linked to a dead run."""
    scope = llm_run_started("crewai", "stale")
    thread = threading.Thread(target=llm_run_finished, args=(scope,))
    thread.start()
    thread.join()
    assert _correlation._current.get() is scope  # still in the variable ...
    assert current_llm_run() is None             # ... but never returned
    _provider_call(inst)
    assert "parent_run_id" not in mem.records[0]
    assert llm_run_finished(scope) is False and llm_run_finished(None) is False


def test_dedupe_provider_drops_only_calls_made_under_a_framework_run(inst, mem):
    set_dedupe("provider")
    scope = llm_run_started("langchain", "run-1")
    assert _provider_call(inst) is None
    assert llm_run_finished(scope) is False
    _provider_call(inst)
    assert len(mem.records) == 1 and "parent_run_id" not in mem.records[0]


def test_dedupe_framework_asks_the_handler_to_drop_only_covered_runs(inst, mem):
    set_dedupe("framework")
    covered = llm_run_started("langchain", "covered")
    _provider_call(inst)
    assert llm_run_finished(covered) is True
    # a run with no provider record beneath it (a model briefcase does not
    # instrument) keeps its framework record
    uncovered = llm_run_started("langchain", "uncovered")
    assert llm_run_finished(uncovered) is False
    assert [r["parent_run_id"] for r in mem.records] == ["covered"]


def test_dedupe_mode_is_validated_and_reported():
    from briefcase.auto import auto, status, undo

    with pytest.raises(ValueError, match="dedupe must be one of"):
        auto("openai", dedupe="both")
    with pytest.raises(ValueError):
        set_dedupe("Framework")
    assert status()["dedupe"] == "none"
    set_dedupe("provider")
    assert status()["dedupe"] == "provider"
    undo()


def test_a_run_whose_end_never_arrives_cannot_capture_later_calls(inst, mem, monkeypatch):
    """An abandoned stream or a failing callback leaves a run open. It must
    not be the parent of every later provider call in that thread (with
    dedupe="provider" those calls would silently vanish)."""
    leaked = llm_run_started("langchain", "leaked")
    following = llm_run_started("langchain", "next")  # a new run supersedes it
    assert leaked.active is False
    _provider_call(inst)
    llm_run_finished(following)
    _provider_call(inst)
    assert [r.get("parent_run_id") for r in mem.records] == ["next", None]

    stale = llm_run_started("langchain", "old")
    monkeypatch.setattr(_correlation, "MAX_RUN_AGE_SECONDS", 0.0)
    assert current_llm_run() is None and stale.active is False
    set_dedupe("provider")
    assert _provider_call(inst) is not None  # recorded, not dropped
