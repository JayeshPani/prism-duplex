"""Fault-injected telemetry I/O races; no RTC, model, or production changes."""
import asyncio
from copy import deepcopy
import json
import threading
import time

import pytest

from agent.coordinator.events import EventBus
from agent.coordinator.coordinator import Coordinator
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.telemetry import fdb_logger
from agent.tools.manifest import Manifest, ToolSpec


class HeldLogger:
    """A real durable logger behind a barrier with a baseline-safe release guard."""
    def __init__(self, phase):
        self.phase = phase
        self.real = fdb_logger.FDBToolLogger("test-room")
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.guard_released = threading.Event()
        self.held_attempt = None

    def hold(self):
        def guard():
            self.guard_released.set()
            self.release.set()

        timer = threading.Timer(1, guard)
        timer.daemon = True
        timer.start()
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            self.release.wait()
        finally:
            timer.cancel()

    def started(self, attempt_id, function, args, t_start):
        if function == "act":
            self.held_attempt = attempt_id
            if self.phase == "started":
                self.hold()
        self.real.started(attempt_id, function, args, t_start)

    def finished(self, attempt_id, t_end, status):
        if attempt_id == self.held_attempt and self.phase == "finished":
            self.hold()
        self.real.finished(attempt_id, t_end, status)


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "attempts.jsonl"
    monkeypatch.setattr(fdb_logger, "TOOL_LOG", str(path))
    return path


def calls(path):
    return [json.loads(line)["call"] for line in path.read_text().splitlines()] if path.exists() else []


def executor(fn, logger, *, changing=True):
    manifest = Manifest("async-telemetry")
    manifest.add(ToolSpec("act", "", {}, [], changing, fn))
    return Executor(manifest, Ledger(), EventBus(), attempt_logger=logger)


async def drain(engine, task, logger=None):
    if logger is not None:
        logger.release.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    assert await engine.aclose(timeout=1)


async def witness_responsive_loop(logger):
    await asyncio.wait_for(logger.entered.wait(), 2)
    await asyncio.sleep(0)
    assert not logger.guard_released.is_set(), "logger blocked the loop until the safety guard released it"
    assert not logger.release.is_set()


@pytest.mark.asyncio
async def test_slow_start_leaves_loop_responsive_and_is_durable_before_backend(log_path):
    logger, observed = HeldLogger("started"), []

    async def act():
        observed.extend(calls(log_path))
        return {"status": "success"}

    engine = executor(act, logger)
    task = asyncio.create_task(engine.run([PlannedCall("write", "act", {})]))
    try:
        await witness_responsive_loop(logger)
        assert observed == [] and calls(log_path) == []
        logger.release.set()
        result = await asyncio.wait_for(task, 2)
        assert len(observed) == 1 and observed[0]["status"] == "unconfirmed"
        assert "timestamp_end" not in observed[0]
        final = calls(log_path)
        assert len(final) == 1 and final[0]["attempt_id"] == observed[0]["attempt_id"]
        assert final[0]["status"] == result.outcomes["write"].status == "done"
    finally:
        await drain(engine, task, logger)


@pytest.mark.asyncio
async def test_slow_finish_leaves_loop_responsive_and_normal_run_awaits_it(log_path):
    logger = HeldLogger("finished")

    async def act():
        return {"status": "success"}

    engine = executor(act, logger)
    task = asyncio.create_task(engine.run([PlannedCall("write", "act", {})]))
    try:
        await witness_responsive_loop(logger)
        assert not task.done()
        assert engine.ledger.summary()[0]["status"] == "done"
        assert calls(log_path)[0]["status"] == "unconfirmed"
        logger.release.set()
        await asyncio.wait_for(task, 2)
        assert calls(log_path)[0]["status"] == "done"
    finally:
        await drain(engine, task, logger)


@pytest.mark.asyncio
@pytest.mark.parametrize("changing", [False, True])
async def test_correction_during_start_never_dispatches_and_finalizes_late_intention(log_path, changing):
    logger, effects, current = HeldLogger("started"), [], [True]

    async def act():
        effects.append("effect")
        return {"status": "success"}

    engine = executor(act, logger, changing=changing)
    task = asyncio.create_task(engine.run([PlannedCall("write", "act", {})], operation_id="old",
                                          is_current=lambda: current[0]))
    try:
        await witness_responsive_loop(logger)
        current[0] = False
        engine.cancel_stale()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert effects == []
        assert engine.ledger.check("act", {}, "old") is None
        logger.release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.outcomes["write"].status == "cancelled"
        newer = await engine.run([PlannedCall("write", "act", {})], operation_id="new")
        assert newer.outcomes["write"].status == "done" and effects == ["effect"]
        assert await engine.aclose(timeout=1)
        rows = calls(log_path)
        assert len(rows) == 2 and len({r["attempt_id"] for r in rows}) == 2
        assert {r["status"] for r in rows} == {"cancelled", "done"}
        cancelled = next(r for r in rows if r["status"] == "cancelled")
        assert "timestamp_end" in cancelled
    finally:
        await drain(engine, task, logger)


@pytest.mark.asyncio
@pytest.mark.parametrize("partially_persisted", [False, True])
async def test_start_failure_never_dispatches_and_preserves_partial_evidence(log_path, partially_persisted):
    logger, effects = fdb_logger.FDBToolLogger("test-room"), []
    real_start = logger.started

    def fail(*args):
        if partially_persisted:
            real_start(*args)
        raise OSError("durability acknowledgement failed")

    logger.started = fail

    async def act():
        effects.append("effect")
        return {"status": "success"}

    engine = executor(act, logger)
    task = asyncio.create_task(engine.run([PlannedCall("write", "act", {})], operation_id="same"))
    try:
        result = await asyncio.wait_for(task, 2)
        assert result.outcomes["write"].status == "error"
        assert effects == [] and engine.ledger.check("act", {}, "same") is None
        assert engine.bus.of_type("tool_log_error")
        rows = calls(log_path)
        assert len(rows) == int(partially_persisted)
        if rows:
            assert rows[0]["status"] == "unconfirmed" and "timestamp_end" not in rows[0]
        logger.started = real_start
        retry = await engine.run([PlannedCall("write", "act", {})], operation_id="same")
        assert retry.outcomes["write"].status == "done" and effects == ["effect"]
    finally:
        await drain(engine, task)


@pytest.mark.asyncio
async def test_parent_cancellation_during_completion_io_preserves_known_success(log_path):
    logger = HeldLogger("finished")

    async def act():
        return {"status": "success"}

    engine = executor(act, logger)
    task = asyncio.create_task(engine.run([PlannedCall("write", "act", {})]))
    try:
        await witness_responsive_loop(logger)
        identity = calls(log_path)[0]["attempt_id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        engine.cancel_stale()
        assert engine.ledger.summary()[0]["status"] == "done"
        logger.release.set()
        assert await engine.aclose(timeout=1)
        assert engine.ledger.summary()[0]["status"] == "done"
        assert not engine.bus.of_type("tool_unknown")
        assert [(r["attempt_id"], r["status"]) for r in calls(log_path)] == [(identity, "done")]
    finally:
        await drain(engine, task, logger)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["started", "finished"])
async def test_close_is_bounded_and_reports_held_io_until_eventual_finalization(log_path, phase):
    logger, effects = HeldLogger(phase), []

    async def act():
        effects.append("effect")
        return {"status": "success"}

    engine = executor(act, logger)
    task = asyncio.create_task(engine.run([PlannedCall("write", "act", {})]))
    try:
        await witness_responsive_loop(logger)
        began = time.monotonic()
        assert not await engine.aclose(timeout=.01)
        assert time.monotonic() - began < .25
        assert not logger.guard_released.is_set()
        assert effects == ([] if phase == "started" else ["effect"])
        assert not engine.bus.of_type("tool_unknown")
        logger.release.set()
        await asyncio.wait_for(task, 2)
        assert await engine.aclose(timeout=1)
        rows = calls(log_path)
        assert len(rows) == 1
        assert rows[0]["status"] == ("cancelled" if phase == "started" else "done")
        assert "timestamp_end" in rows[0]
    finally:
        await drain(engine, task, logger)


@pytest.mark.asyncio
async def test_resolved_mutable_arguments_are_frozen_for_log_and_backend(log_path):
    logger, shared, observed = HeldLogger("started"), {"destination": "airport"}, []

    async def lookup():
        return {"status": "success", "payload": shared}

    async def act(payload):
        observed.append(deepcopy(payload))
        return {"status": "success"}

    manifest = Manifest("mutable-result")
    manifest.add(ToolSpec("lookup", "", {}, [], False, lookup))
    manifest.add(ToolSpec("act", "", {"payload": {}}, ["payload"], True, act))
    engine = Executor(manifest, Ledger(), EventBus(), attempt_logger=logger)
    task = asyncio.create_task(engine.run([PlannedCall("lookup", "lookup", {}),
                                          PlannedCall("write", "act", {"payload": "$lookup.payload"})]))
    try:
        await witness_responsive_loop(logger)
        shared["destination"] = "office"
        logger.release.set()
        result = await asyncio.wait_for(task, 2)
        assert observed == [{"destination": "airport"}]
        write = next(r for r in calls(log_path) if r["function"] == "act")
        assert write["args"] == result.outcomes["write"].args == {"payload": {"destination": "airport"}}
        assert engine.ledger.summary()[0]["args"] == write["args"]
    finally:
        await drain(engine, task, logger)


@pytest.mark.asyncio
async def test_current_version_is_rechecked_after_start_persistence(log_path):
    logger, current, effects = fdb_logger.FDBToolLogger("test-room"), [True], []
    real_start, loop = logger.started, asyncio.get_running_loop()

    def persist_then_correct(*args):
        real_start(*args)
        loop.call_soon_threadsafe(current.__setitem__, 0, False)

    logger.started = persist_then_correct

    async def act():
        effects.append("effect")
        return {"status": "success"}

    engine = executor(act, logger)
    task = asyncio.create_task(engine.run([PlannedCall("write", "act", {})],
                                          is_current=lambda: current[0]))
    try:
        result = await asyncio.wait_for(task, 2)
        assert effects == [] and result.outcomes["write"].status == "cancelled"
        assert await engine.aclose(timeout=1)
        assert len(calls(log_path)) == 1 and calls(log_path)[0]["status"] == "cancelled"
    finally:
        await drain(engine, task)


@pytest.mark.asyncio
async def test_coordinator_logger_only_shutdown_does_not_claim_unknown_backend_effect(log_path):
    logger = HeldLogger("finished")

    async def act():
        return {"status": "success"}

    async def speak(_text):
        raise AssertionError("shutdown must not start speech")

    engine = executor(act, logger)
    coord = Coordinator(None, engine, engine.ledger, engine.bus, speak)
    task = asyncio.create_task(engine.run([PlannedCall("write", "act", {})]))
    try:
        await witness_responsive_loop(logger)
        await asyncio.wait_for(coord.aclose(timeout=.01), .25)
        assert engine.ledger.summary()[0]["status"] == "done"
        assert not engine.bus.of_type("tool_unknown")
        assert engine.bus.of_type("tool_error")
        assert engine.bus.of_type("tool_log_error")
        logger.release.set()
        assert await engine.aclose(timeout=1)
        assert calls(log_path)[0]["status"] == "done"
    finally:
        await drain(engine, task, logger)


@pytest.mark.asyncio
@pytest.mark.parametrize("change_during", ["start_persistence", "backend_read"])
async def test_read_cache_does_not_reuse_result_from_a_changed_context(log_path, change_during):
    logger = HeldLogger("started" if change_during == "start_persistence" else "none")
    state, observed = {"scope": "A"}, []
    backend_entered, release_backend = asyncio.Event(), asyncio.Event()

    async def act():
        if change_during == "backend_read" and not observed:
            backend_entered.set()
            await release_backend.wait()
        observed.append(state["scope"])
        return {"status": "success", "scope": state["scope"]}

    engine = executor(act, logger, changing=False)
    engine.manifest.tools["act"].cache_context = lambda: state["scope"]
    task = asyncio.create_task(engine.run([PlannedCall("first", "act", {})]))
    try:
        if change_during == "start_persistence":
            await witness_responsive_loop(logger)
        else:
            await asyncio.wait_for(backend_entered.wait(), 2)
        assert observed == []
        state["scope"] = "B"
        logger.release.set()
        release_backend.set()
        first = await asyncio.wait_for(task, 2)
        assert first.outcomes["first"].result["scope"] == "B"

        state["scope"] = "A"
        second = await asyncio.wait_for(engine.run([PlannedCall("second", "act", {})]), 2)
        assert second.outcomes["second"].status == "done"
        assert second.outcomes["second"].result["scope"] == "A"
        assert observed == ["B", "A"]
        repeated = await asyncio.wait_for(engine.run([PlannedCall("third", "act", {})]), 2)
        assert repeated.outcomes["third"].status == "reused"
        assert repeated.outcomes["third"].result["scope"] == "A"
        assert observed == ["B", "A"]
    finally:
        release_backend.set()
        await drain(engine, task, logger)
