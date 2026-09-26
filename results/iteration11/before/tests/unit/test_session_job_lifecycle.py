"""Exercise the production entrypoint without importing audio engines or LiveKit.

The SDK fakes model its documented one-way shutdown relationship: a session
close does not finish a job, and a job shutdown closes the session before
running the registered cleanup callbacks. Actual RTC closure is measured
separately; these tests protect PRISM's connection between those lifecycles.
"""

import ast
import asyncio
from enum import Enum
import json
import logging
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agent.coordinator import events as E
from agent.coordinator.coordinator import Coordinator, CoordinatorConfig
from agent.coordinator.executor import Executor
from agent.coordinator.ledger import Ledger
from agent.coordinator.resolver import IntentResolver
from agent.coordinator.responder import Responder
from agent.tools.manifest import Manifest


class CloseReason(Enum):
    PARTICIPANT_DISCONNECTED = "participant_disconnected"
    ERROR = "error"
    JOB_SHUTDOWN = "job_shutdown"


class Session:
    def __init__(self, **kwargs):
        self.handlers = {}
        self.started = False
        self.lock = asyncio.Lock()
        self.close_during_start = False

    def on(self, event):
        def register(callback):
            self.handlers.setdefault(event, []).append(callback)
            return callback
        return register

    async def start(self, *, room, agent):
        self.agent = agent
        self.started = True
        if self.close_during_start:
            await self.aclose(CloseReason.PARTICIPANT_DISCONNECTED)

    async def aclose(self, reason=CloseReason.JOB_SHUTDOWN):
        # AgentSession 1.8.3 sets _started=False then emits close under its lock.
        async with self.lock:
            if not self.started:
                return
            self.started = False
            for callback in self.handlers.get("close", []):
                callback(SimpleNamespace(reason=reason))
            await asyncio.sleep(0)


class Context:
    def __init__(self, name):
        self.shutdown_requested = asyncio.Event()
        self.reasons = []
        self.callbacks = []
        self.publish_started = asyncio.Event()
        self.publish_cancelled = asyncio.Event()
        self.room = SimpleNamespace(name=name, local_participant=SimpleNamespace(
            publish_data=self.publish_data))

    async def publish_data(self, *args, **kwargs):
        self.publish_started.set()
        try:
            await asyncio.Future()
        finally:
            self.publish_cancelled.set()

    def shutdown(self, reason):
        self.reasons.append(reason)
        self.shutdown_requested.set()

    def add_shutdown_callback(self, callback):
        self.callbacks.append(callback)

    async def finish_shutdown(self, session):
        await self.shutdown_requested.wait()
        await session.aclose()
        await asyncio.gather(*(callback() for callback in self.callbacks))


@pytest_asyncio.fixture
async def start_room(tmp_path):
    rooms = []

    async def start(*, close_during_start=False):
        ctx = Context(f"car-test-{len(rooms)}")
        session = Session()
        session.close_during_start = close_during_start
        client = SimpleNamespace(client=SimpleNamespace(close=AsyncMock()))
        planning_started, planning_cancelled = asyncio.Event(), asyncio.Event()

        async def complete_json(*args):
            planning_started.set()
            try:
                await asyncio.Future()
            finally:
                planning_cancelled.set()

        client.complete_json = complete_json
        opened = []

        def tracked_open(*args, **kwargs):
            handle = open(*args, **kwargs)
            opened.append(handle)
            return handle

        source = Path(__file__).resolve().parents[2] / "agent/main.py"
        tree = ast.parse(source.read_text())
        entry = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name == "entrypoint")
        entry.decorator_list = []
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
            ast.alias(name="annotations")], level=0), entry], type_ignores=[])
        namespace = {
            "asyncio": asyncio, "json": json, "time": time, "open": tracked_open,
            "log": logging.getLogger(__name__), "E": E, "EventBus": E.EventBus,
            "Ledger": Ledger, "Executor": Executor, "Coordinator": Coordinator,
            "IntentResolver": IntentResolver, "Responder": Responder,
            "prewarm": lambda: None, "build_manifest": lambda room: ("car", Manifest("test")),
            "make_llm": lambda cfg: client, "AgentSession": lambda **kw: session,
            "LocalSTT": lambda *a, **kw: SimpleNamespace(transcribe_since=AsyncMock(return_value=None)),
            "local_turn_handling": lambda: object(), "_VAD": object(), "_TTS": object(),
            "DuplexAgent": lambda coord: SimpleNamespace(coordinator=coord),
            "make_coordinator_config": lambda *a, **kw: CoordinatorConfig(ack_after_ms=60_000),
            "CFG": {"profile": "test", "stt": {"kind": "test", "model": "test"},
                    "llm": {"model": "test"}},
            "TRACE_DIR": tmp_path / "traces",
            "fdb_logger": SimpleNamespace(HEARTBEAT_LOG=tmp_path / "heartbeat.log",
                                          FDBToolLogger=lambda room: None),
        }
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        await namespace["entrypoint"](ctx)
        room = SimpleNamespace(ctx=ctx, session=session, client=client, opened=opened,
                               coord=session.agent.coordinator,
                               planning_started=planning_started,
                               planning_cancelled=planning_cancelled)
        rooms.append(room)
        return room

    yield start
    # Release resources even when the intentionally red baseline fails first.
    for room in rooms:
        await room.session.aclose()
        for callback in room.ctx.callbacks:
            if not room.opened[-1].closed:
                await callback()


@pytest.mark.asyncio
async def test_returning_from_started_entrypoint_keeps_conversation_live(start_room):
    room = await start_room()
    assert room.session.started
    assert not room.ctx.shutdown_requested.is_set()
    room.client.client.close.assert_not_awaited()
    assert not room.opened[-1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [CloseReason.PARTICIPANT_DISCONNECTED,
                                   CloseReason.ERROR, CloseReason.JOB_SHUTDOWN])
async def test_session_close_requests_job_shutdown_and_drains_resources(start_room, reason):
    room = await start_room()
    room.coord.on_user_turn("Navigate to the airport.")
    await asyncio.wait_for(room.planning_started.wait(), 1)
    await asyncio.wait_for(room.ctx.publish_started.wait(), 1)
    job = asyncio.create_task(room.ctx.finish_shutdown(room.session))
    try:
        await room.session.aclose(reason)
        assert room.ctx.shutdown_requested.is_set(), "closed session left its job running"
        assert reason.value in room.ctx.reasons[0]
        await asyncio.wait_for(job, 1)
        assert room.planning_cancelled.is_set()
        assert room.ctx.publish_cancelled.is_set()
        room.client.client.close.assert_awaited_once()
        assert all(handle.closed for handle in room.opened)
        previous_events = list(room.coord.bus.history)
        room.coord.on_user_turn("A late transport callback.")
        assert room.coord.bus.history == previous_events
        await room.session.aclose()
        assert len(room.ctx.reasons) == 1
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_during_session_start_is_not_missed(start_room):
    room = await start_room(close_during_start=True)
    assert room.ctx.shutdown_requested.is_set(), "close listener was installed too late"
    assert not room.coord.bus.of_type("session_started")
    await asyncio.wait_for(room.ctx.finish_shutdown(room.session), 1)
    room.client.client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_closing_one_room_does_not_shutdown_another(start_room):
    first, second = await start_room(), await start_room()
    await first.session.aclose(CloseReason.PARTICIPANT_DISCONNECTED)
    assert first.ctx.shutdown_requested.is_set()
    await asyncio.wait_for(first.ctx.finish_shutdown(first.session), 1)
    assert second.session.started
    assert not second.ctx.shutdown_requested.is_set()
    second.client.client.close.assert_not_awaited()
    assert not second.opened[-1].closed
