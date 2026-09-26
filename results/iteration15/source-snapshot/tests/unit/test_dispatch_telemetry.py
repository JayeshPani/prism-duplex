"""Dispatch evidence survives slow tools, one-shot collection and worker exits."""

import ast
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent.coordinator.events import EventBus, TOOL_STARTED
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.telemetry import fdb_logger
from agent.tools.manifest import Manifest, ToolSpec

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "tool_calls.jsonl"
    monkeypatch.setattr(fdb_logger, "TOOL_LOG", str(path))
    return path


def make_executor(fn, *, state=True, cache=None, room="eval-room"):
    manifest = Manifest("dispatch-evidence")
    manifest.add(ToolSpec("act", "", {}, [], state, fn, cache_context=cache))
    return Executor(manifest, Ledger(), EventBus(), attempt_logger=fdb_logger.FDBToolLogger(room))


def collect_with_upstream(path, *, room="eval-room", stream_start=100):
    """Execute the unmodified runner's collection block without loading models."""
    source = ROOT / "bench/Full-Duplex-Bench/v3/run_tool_benchmark.py"
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for index, statement in enumerate(node.body):
            if (isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name)
                    and statement.targets[0].id == "actual_tool_calls"):
                # actual calls initialization, stream start, collection, result assignment
                block = ast.Module(body=node.body[index:index + 4], type_ignores=[])
                context = {"Path": lambda _: path, "json": json, "room_name": room,
                           "result": {"stream_start_time": stream_start}}
                exec(compile(block, str(source), "exec"), context)
                return context["result"]["actual_tool_calls"]
    raise AssertionError("upstream tool collection block was not found")


@pytest.mark.asyncio
async def test_running_backend_is_collected_then_finalized_without_duplicate(log_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def act():
        entered.set()
        await release.wait()
        return {"status": "success"}

    engine = make_executor(act)
    run = asyncio.create_task(engine.run([PlannedCall("c1", "act", {})]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        running = collect_with_upstream(log_path)
        assert len(running) == 1
        assert running[0]["function"] == "act"
        assert running[0]["status"] == "unconfirmed"
        assert "timestamp_end" not in running[0]
        # A reader holding an earlier snapshot cannot see a half-written update.
        with log_path.open() as reader:
            release.set()
            result = await asyncio.wait_for(run, 1)
            old = [json.loads(line) for line in reader]
        assert old[0]["call"]["status"] == "unconfirmed"
        finished = collect_with_upstream(log_path)
        assert len(finished) == 1
        assert finished[0]["attempt_id"] == running[0]["attempt_id"]
        assert finished[0]["status"] == result.outcomes["c1"].status == "done"
        assert finished[0]["timestamp_end"] >= finished[0]["timestamp_start"]
        assert finished[0]["end_evidence"] == "executor_observation_ended"
    finally:
        release.set()
        await run


@pytest.mark.asyncio
async def test_stale_before_final_dispatch_boundary_has_no_record(log_path):
    effects = []

    async def act():
        effects.append(1)
        return {"status": "success"}

    engine = make_executor(act)
    current = True

    def correct(event):
        nonlocal current
        if event.type == TOOL_STARTED:
            current = False

    engine.bus.subscribe(correct)
    result = await engine.run([PlannedCall("c1", "act", {})], is_current=lambda: current)
    assert result.outcomes["c1"].status == "cancelled"
    assert effects == []
    assert collect_with_upstream(log_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state,status", [(True, "blocked"), (False, "reused")])
async def test_non_dispatch_reuse_and_block_do_not_add_attempts(log_path, state, status):
    async def act():
        return {"status": "success"}

    engine = make_executor(act, state=state, cache=None if state else lambda: "same")
    calls = [PlannedCall("c1", "act", {})]
    await engine.run(calls, operation_id="same-intent")
    result = await engine.run(calls, operation_id="same-intent")
    assert result.outcomes["c1"].status == status
    assert len(collect_with_upstream(log_path)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response,state,status", [
    ("exception", True, "unknown"), ("exception", False, "error"),
    ("cancel", True, "unknown"), ("cancel", False, "cancelled"),
    ("error", True, "error"),
])
async def test_failed_or_cancelled_invocations_are_preserved(log_path, response, state, status):
    async def act():
        if response == "exception":
            raise TimeoutError("acknowledgement lost")
        if response == "cancel":
            raise asyncio.CancelledError
        return {"status": "error", "error": "rejected"}

    engine = make_executor(act, state=state)
    result = await engine.run([PlannedCall("c1", "act", {})])
    calls = collect_with_upstream(log_path)
    assert len(calls) == 1
    assert calls[0]["status"] == result.outcomes["c1"].status == status
    assert isinstance(calls[0]["timestamp_end"], (int, float))


@pytest.mark.asyncio
async def test_shutdown_cannot_invent_an_end_for_a_still_running_backend(log_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def act():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"status": "success"}

    engine = make_executor(act)
    run = asyncio.create_task(engine.run([PlannedCall("c1", "act", {})]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not await engine.aclose(timeout=0.01)
        calls = collect_with_upstream(log_path)
        assert len(calls) == 1
        assert calls[0]["status"] == "unconfirmed"
        assert "timestamp_end" not in calls[0]
    finally:
        release.set()
        await run


@pytest.mark.asyncio
async def test_start_logging_failure_prevents_unobservable_dispatch_and_allows_retry(log_path, monkeypatch):
    effects = []

    async def act():
        effects.append(1)
        return {"status": "success"}

    engine = make_executor(act)
    real_start = engine.attempt_logger.started

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(engine.attempt_logger, "started", fail)
    calls = [PlannedCall("c1", "act", {})]
    failed = await engine.run(calls, operation_id="same-intent")
    assert failed.outcomes["c1"].status == "error"
    assert "not dispatched" in failed.outcomes["c1"].error
    assert effects == engine.ledger.summary() == []
    assert engine.bus.of_type("tool_log_error")
    monkeypatch.setattr(engine.attempt_logger, "started", real_start)
    retried = await engine.run(calls, operation_id="same-intent")
    assert retried.outcomes["c1"].status == "done"
    assert effects == [1]
    assert len(collect_with_upstream(log_path)) == 1


@pytest.mark.asyncio
async def test_completion_logging_failure_preserves_intention_and_success(log_path, monkeypatch):
    async def act():
        return {"status": "success"}

    engine = make_executor(act)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(engine.attempt_logger, "finished", fail)
    result = await engine.run([PlannedCall("c1", "act", {})])
    assert result.outcomes["c1"].status == "done"
    assert engine.bus.of_type("tool_log_error")
    calls = collect_with_upstream(log_path)
    assert len(calls) == 1 and calls[0]["status"] == "unconfirmed"
    assert "timestamp_end" not in calls[0]


def test_process_exit_inside_backend_leaves_collectable_unconfirmed_attempt(log_path):
    script = '''
import asyncio, os
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.telemetry.fdb_logger import FDBToolLogger
from agent.tools.manifest import Manifest, ToolSpec
async def act():
    os._exit(17)
m = Manifest("crash"); m.add(ToolSpec("act", "", {}, [], True, act))
e = Executor(m, Ledger(), EventBus(), attempt_logger=FDBToolLogger("eval-room"))
asyncio.run(e.run([PlannedCall("c1", "act", {})]))
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            env={**os.environ, "FDB_TOOL_LOG": str(log_path)}, timeout=5)
    assert result.returncode == 17
    calls = collect_with_upstream(log_path)
    assert len(calls) == 1 and calls[0]["status"] == "unconfirmed"
    assert "timestamp_end" not in calls[0]


def test_concurrent_worker_updates_preserve_all_attempts_and_legacy_rows(log_path):
    fdb_logger.log_tool_call("legacy-room", "old", {}, 1, 2)
    script = '''
import sys
from agent.telemetry.fdb_logger import FDBToolLogger
room = sys.argv[1]
logger = FDBToolLogger(room)
for index in range(8):
    identity = f"{room}:{index}"
    logger.started(identity, "act", {"index": index}, 110 + index)
    logger.finished(identity, 111 + index, "done")
'''
    workers = [subprocess.Popen([sys.executable, "-c", script, room], cwd=ROOT,
                               env={**os.environ, "FDB_TOOL_LOG": str(log_path)})
               for room in ("room-a", "room-b", "room-c")]
    for worker in workers:
        assert worker.wait(timeout=5) == 0
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(rows) == 25
    assert collect_with_upstream(log_path, room="legacy-room", stream_start=0) == [
        {"function": "old", "args": {}, "timestamp_start": 1, "timestamp_end": 2},
    ]
    for room in ("room-a", "room-b", "room-c"):
        calls = collect_with_upstream(log_path, room=room)
        assert len(calls) == 8
        assert {call["args"]["index"] for call in calls} == set(range(8))
        assert all(call["status"] == "done" for call in calls)
