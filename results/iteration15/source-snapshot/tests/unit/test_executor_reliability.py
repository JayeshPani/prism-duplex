"""Independent development cases for dispatch, cancellation and truthful outcomes."""

import asyncio

import pytest

from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger, action_key
from agent.coordinator.refs import UnresolvedRef, deps_of, resolve_args
from agent.coordinator.responder import Responder
from agent.tools.manifest import Manifest, ToolSpec


def executor(fn, *, state=True):
    manifest = Manifest("reliability")
    manifest.add(ToolSpec("act", "", {"id": {}, "destination": {"type": "string"},
                                     "x": {}, "nested": {}}, [], state, fn))
    logged = []
    engine = Executor(manifest, Ledger(), EventBus(), lambda *entry: logged.append(entry))
    return engine, logged


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [
    [PlannedCall("c1", "act", {}), PlannedCall("c1", "act", {})],
    [PlannedCall("c1", "act", {}), PlannedCall("c2", "act", {"x": "$missing.id"})],
    [PlannedCall("c1", "act", {"nested": [{"x": "$missing.id"}]})],
    [PlannedCall("c1", "act", {"x": "$c1.id"})],
    [PlannedCall("c1", "act", {"x": "$c2.id"}), PlannedCall("c2", "act", {"x": "$c1.id"})],
    [PlannedCall("c1", "act", {}), PlannedCall("c2", "missing_tool", {})],
])
async def test_invalid_plan_has_no_partial_effects(calls):
    effects = []

    async def act(**args):
        effects.append(args)
        return {"id": "value"}

    engine, logged = executor(act)
    result = await asyncio.wait_for(engine.run(calls), timeout=0.1)
    assert effects == []
    assert logged == []
    assert result.outcomes and all(o.status == "error" for o in result.outcomes.values())


def test_nested_references_are_dependencies_and_resolve_recursively():
    args = {"payload": [{"id": "$c1.id"}], "direct": "$c2"}
    assert deps_of(args) == {"c1", "c2"}
    assert resolve_args(args, {"c1": {"id": "ABC-123"}, "c2": 3}) == {
        "payload": [{"id": "ABC-123"}], "direct": 3,
    }


def test_wrong_reference_path_cannot_silently_choose_first_item():
    with pytest.raises(UnresolvedRef):
        resolve_args({"id": "$c1.id"}, {"c1": {"items": [{"id": "first"}, {"id": "second"}]}})


@pytest.mark.parametrize("first,second", [("ABC-123", "ABC_123"), ("ABC123", "abc123"), ("ABC123", " ABC123 ")])
def test_opaque_identifiers_are_not_lossily_canonicalized(first, second):
    assert action_key("act", {"id": first}) != action_key("act", {"id": second})


@pytest.mark.asyncio
async def test_explicit_failure_is_retryable_and_not_reported_done():
    attempts = []

    async def act():
        attempts.append(1)
        return {"status": "error", "error": "backend rejected request"}

    engine, logged = executor(act)
    first = await engine.run([PlannedCall("c1", "act", {})])
    second = await engine.run([PlannedCall("c1", "act", {})])
    assert first.outcomes["c1"].status == second.outcomes["c1"].status == "error"
    assert engine.ledger.summary() == []
    assert len(attempts) == len(logged) == 2


@pytest.mark.asyncio
async def test_error_result_cannot_feed_dependent_write():
    effects = []

    async def act(**args):
        effects.append(args)
        return {"status": "error", "id": "unusable"}

    engine, logged = executor(act)
    result = await engine.run([PlannedCall("c1", "act", {}), PlannedCall("c2", "act", {"id": "$c1.id"})])
    assert effects == [{}]
    assert result.outcomes["c2"].status == "skipped"
    assert len(logged) == 1


@pytest.mark.asyncio
async def test_write_exception_is_unknown_logged_and_not_retried():
    effects = []

    async def act():
        effects.append("backend may have committed")
        raise TimeoutError("acknowledgment lost")

    engine, logged = executor(act)
    first = await engine.run([PlannedCall("c1", "act", {})])
    second = await engine.run([PlannedCall("c1", "act", {})])
    assert first.outcomes["c1"].status == second.outcomes["c1"].status == "unknown"
    assert len(effects) == len(logged) == 1
    assert engine.ledger.summary()[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_new_intention_can_repeat_but_same_operation_retry_cannot():
    effects = []

    async def act():
        effects.append(1)
        return {"status": "success"}

    engine, logged = executor(act)
    calls = [PlannedCall("c1", "act", {})]
    first = await engine.run(calls, operation_id="first-visit")
    retry = await engine.run(calls, operation_id="first-visit")
    second = await engine.run(calls, operation_id="return-visit")
    assert [x.outcomes["c1"].status for x in (first, retry, second)] == ["done", "blocked", "done"]
    assert len(effects) == len(logged) == 2


@pytest.mark.asyncio
async def test_separate_runs_default_to_new_intentions():
    effects = []

    async def act():
        effects.append(1)
        return {"status": "success"}

    engine, _ = executor(act)
    await engine.run([PlannedCall("c1", "act", {})])
    await engine.run([PlannedCall("c1", "act", {})])
    assert len(effects) == 2


@pytest.mark.asyncio
async def test_cancel_reaches_all_executions_and_logs_attempts():
    started = asyncio.Queue()
    release = asyncio.Event()

    async def act(**args):
        await started.put(args)
        await release.wait()
        return {"status": "success"}

    engine, logged = executor(act, state=False)
    runs = [asyncio.create_task(engine.run([PlannedCall("c1", "act", {"id": i})])) for i in range(2)]
    try:
        for _ in runs:
            await asyncio.wait_for(started.get(), 0.2)
        assert engine.cancel_stale() == 2
        results = await asyncio.wait_for(asyncio.gather(*runs), 0.2)
        assert [r.outcomes["c1"].status for r in results] == ["cancelled", "cancelled"]
        assert len(logged) == 2
        assert len({r.execution_id for r in results}) == 2
    finally:
        release.set()
        await asyncio.gather(*runs, return_exceptions=True)


@pytest.mark.asyncio
async def test_version_is_checked_immediately_before_dispatch():
    effects = []

    async def act():
        effects.append(1)
        return {"status": "success"}

    engine, logged = executor(act)
    result = await engine.run([PlannedCall("c1", "act", {})], is_current=lambda: False)
    assert effects == logged == []
    assert result.outcomes["c1"].status == "cancelled"


@pytest.mark.asyncio
async def test_started_write_finishes_but_its_stale_dependent_does_not_start():
    started, release = asyncio.Event(), asyncio.Event()
    effects = []
    current = True

    async def act(**args):
        effects.append(args)
        started.set()
        await release.wait()
        return {"status": "success", "id": "value"}

    engine, logged = executor(act)
    run = asyncio.create_task(engine.run([
        PlannedCall("c1", "act", {}), PlannedCall("c2", "act", {"id": "$c1.id"}),
    ], is_current=lambda: current))
    try:
        await asyncio.wait_for(started.wait(), 0.2)
        current = False
        engine.cancel_stale()
        release.set()
        result = await asyncio.wait_for(run, 0.2)
        assert effects == [{}]
        assert result.outcomes["c1"].status == "done"
        assert result.outcomes["c2"].status == "cancelled"
        assert len(logged) == 1
    finally:
        release.set()
        await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_marks_cancelled_write_unknown_and_logs_it():
    started = asyncio.Event()

    async def act():
        started.set()
        await asyncio.Event().wait()

    engine, logged = executor(act)
    run = asyncio.create_task(engine.run([PlannedCall("c1", "act", {})]))
    try:
        await asyncio.wait_for(started.wait(), 0.2)
        await asyncio.wait_for(engine.aclose(timeout=0.01), 0.2)
        result = await asyncio.wait_for(run, 0.2)
        assert result.outcomes["c1"].status == "unknown"
        assert engine.ledger.summary()[0]["status"] == "unknown"
        assert len(logged) == 1
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
async def test_dynamic_reads_are_not_cached_without_explicit_context():
    async def act():
        return {"status": "success"}

    engine, logged = executor(act, state=False)
    await engine.run([PlannedCall("c1", "act", {})])
    await engine.run([PlannedCall("c1", "act", {})])
    assert len(logged) == 2


@pytest.mark.asyncio
async def test_correction_from_start_event_prevents_actual_dispatch():
    effects = []
    current = True

    async def act():
        effects.append(1)
        return {"status": "success"}

    engine, logged = executor(act)

    def correct(event):
        nonlocal current
        if event.type == "tool_started":
            current = False

    engine.bus.subscribe(correct)
    result = await engine.run([PlannedCall("c1", "act", {})], is_current=lambda: current)
    assert effects == logged == []
    assert result.outcomes["c1"].status == "cancelled"


@pytest.mark.asyncio
async def test_late_read_result_keeps_its_original_cache_context():
    started, release = asyncio.Event(), asyncio.Event()
    context = {"version": 1}

    async def act():
        version = context["version"]
        started.set()
        await release.wait()
        return {"status": "success", "version": version}

    engine, logged = executor(act, state=False)
    engine.manifest.tools["act"].cache_context = lambda: context
    first = asyncio.create_task(engine.run([PlannedCall("c1", "act", {})]))
    await asyncio.wait_for(started.wait(), 0.2)
    context["version"] = 2
    release.set()
    await first
    second = await engine.run([PlannedCall("c1", "act", {})])
    cached = await engine.run([PlannedCall("c1", "act", {})])
    assert second.outcomes["c1"].result["version"] == 2
    assert cached.outcomes["c1"].status == "reused"
    assert len(logged) == 2


@pytest.mark.asyncio
async def test_new_write_waits_for_dispatched_old_write_to_finish():
    started, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def act(destination):
        if destination == "airport":
            started.set()
            await release.wait()
        effects.append(destination)
        return {"status": "success"}

    engine, logged = executor(act)
    old = asyncio.create_task(engine.run([PlannedCall("c1", "act", {"destination": "airport"})]))
    await asyncio.wait_for(started.wait(), 0.2)
    engine.cancel_stale()
    new = asyncio.create_task(engine.run([PlannedCall("c1", "act", {"destination": "office"})]))
    await asyncio.sleep(0)
    assert effects == []
    release.set()
    await asyncio.wait_for(asyncio.gather(old, new), 0.2)
    assert effects == ["airport", "office"]
    assert len(logged) == 2


@pytest.mark.asyncio
async def test_cancelling_run_caller_does_not_cancel_dispatched_write():
    started, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def act():
        started.set()
        await release.wait()
        effects.append(1)
        return {"status": "success"}

    engine, logged = executor(act)
    run = asyncio.create_task(engine.run([PlannedCall("c1", "act", {})]))
    await asyncio.wait_for(started.wait(), 0.2)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert effects == logged == []
    release.set()
    assert await engine.aclose(timeout=0.2)
    assert effects == [1] and len(logged) == 1
    assert engine.ledger.summary()[0]["status"] == "done"


@pytest.mark.asyncio
async def test_logging_failure_cannot_change_a_successful_write_outcome():
    async def act():
        return {"status": "success"}

    engine, _ = executor(act)

    def broken_logger(*args):
        raise OSError("disk full")

    engine.tool_logger = broken_logger
    result = await engine.run([PlannedCall("c1", "act", {})])
    assert result.outcomes["c1"].status == "done"
    assert engine.ledger.summary()[0]["status"] == "done"
    assert engine.bus.of_type("tool_log_error")


@pytest.mark.asyncio
async def test_unknown_outcome_speech_is_deterministic():
    async def act():
        raise TimeoutError("lost acknowledgment")

    engine, _ = executor(act)
    result = await engine.run([PlannedCall("c1", "act", {})])
    # No language-model inference is needed to state the uncertainty.
    responder = Responder(None)
    assert await responder.summarize("perform action", result) == (
        "I couldn't confirm whether that action completed, so I haven't retried it."
    )


@pytest.mark.asyncio
async def test_shutdown_reports_unknown_if_backend_suppresses_cancellation():
    started, release = asyncio.Event(), asyncio.Event()

    async def act():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"status": "success"}

    engine, logged = executor(act)
    run = asyncio.create_task(engine.run([PlannedCall("c1", "act", {})]))
    try:
        await asyncio.wait_for(started.wait(), 0.2)
        assert not await engine.aclose(timeout=0.01)
        assert engine.ledger.summary()[0]["status"] == "unknown"
        assert logged == []  # Invocation is still in progress; no fabricated end time.
    finally:
        release.set()
        await asyncio.wait_for(run, 0.2)
    assert engine.ledger.summary()[0]["status"] == "done"
    assert len(logged) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first_status", ["success", "error"])
async def test_operation_retry_cannot_change_write_arguments(first_status):
    effects = []
    options = iter(["original-id", "replacement-id"])

    async def lookup():
        return {"id": next(options)}

    async def act(id):
        effects.append(id)
        return {"status": first_status}

    engine, logged = executor(act)
    engine.manifest.add(ToolSpec("lookup", "", {}, [], False, lookup))
    calls = [PlannedCall("c1", "lookup", {}), PlannedCall("c2", "act", {"id": "$c1.id"})]
    await engine.run(calls, operation_id="same-intention")
    retry = await engine.run(calls, operation_id="same-intention")
    assert effects == ["original-id"]
    assert retry.outcomes["c2"].status == "error"
    assert "identity" in retry.outcomes["c2"].error
    assert len(logged) == 3


@pytest.mark.asyncio
async def test_same_operation_can_retry_identical_explicitly_failed_action():
    attempts = []

    async def act():
        attempts.append(1)
        return {"status": "error" if len(attempts) == 1 else "success"}

    engine, logged = executor(act)
    calls = [PlannedCall("c1", "act", {})]
    first = await engine.run(calls, operation_id="retryable-operation")
    second = await engine.run(calls, operation_id="retryable-operation")
    assert first.outcomes["c1"].status == "error"
    assert second.outcomes["c1"].status == "done"
    assert len(logged) == 2
