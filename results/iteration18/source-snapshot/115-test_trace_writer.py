"""Thread-barrier checks of ordered per-room trace persistence and shutdown."""
import asyncio
import json
import logging
from pathlib import Path
import threading

import pytest

from agent.coordinator.events import Event, EventBus
from agent.telemetry.trace_writer import TraceWriter


class HeldIO:
    """Hold one native boundary; release even a broken synchronous baseline."""

    def __init__(self, path, phase, monkeypatch, *, fail=None):
        self.path, self.phase, self.fail = path, phase, fail
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.guard_released = threading.Event()
        self.calls = []
        self.held = False
        self.file = None
        original_mkdir, original_open = Path.mkdir, Path.open

        def mkdir(path, *args, **kwargs):
            if path == self.path.parent:
                self.boundary("mkdir")
            return original_mkdir(path, *args, **kwargs)

        def open_file(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path != self.path or mode == "r":
                return original_open(path, *args, **kwargs)
            self.boundary("open")
            self.file = original_open(path, *args, **kwargs)
            return FileProxy(self.file, self)

        monkeypatch.setattr(Path, "mkdir", mkdir)
        monkeypatch.setattr(Path, "open", open_file)

    def boundary(self, phase):
        self.calls.append((phase, threading.get_ident()))
        if phase != self.phase or self.held:
            return
        self.held = True

        def guard():
            self.guard_released.set()
            self.release.set()

        timer = threading.Timer(2, guard)
        timer.daemon = True
        timer.start()
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            self.release.wait()
        finally:
            timer.cancel()

    async def witness_responsive_loop(self):
        await asyncio.wait_for(self.entered.wait(), 3)
        await asyncio.sleep(0)
        assert not self.guard_released.is_set(), "file operation blocked the loop until the safety release"
        assert not self.release.is_set()
        assert all(thread != self.loop_thread for _, thread in self.calls)


class FileProxy:
    def __init__(self, file, held):
        self.file, self.held = file, held

    def write(self, payload):
        self.held.boundary("write")
        if self.held.fail == "write":
            self.file.write(payload[:len(payload) // 2])
            self.file.flush()
            raise OSError("injected partial trace write")
        return self.file.write(payload)

    def flush(self):
        self.held.boundary("flush")
        self.file.flush()
        if self.held.fail == "flush":
            raise OSError("injected trace flush acknowledgment failure")

    def close(self):
        self.held.boundary("close")
        return self.file.close()


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["mkdir", "open", "write", "flush", "close"])
async def test_slow_native_boundary_keeps_loop_responsive(tmp_path, monkeypatch, phase):
    path = tmp_path / "room" / "trace.jsonl"
    held = HeldIO(path, phase, monkeypatch)
    writer = TraceWriter(path)

    async def lifecycle():
        await writer.start()
        writer(Event("observation", {"value": 7}, ts=1))
        return await writer.aclose()

    task = asyncio.create_task(lifecycle())
    try:
        await held.witness_responsive_loop()
        assert not task.done()
        held.release.set()
        assert await asyncio.wait_for(task, 3)
        assert records(path) == [{"type": "observation", "data": {"value": 7}, "ts": 1}]
        assert (writer.accepted, writer.written, writer.rejected) == (1, 1, 0)
        assert held.file.closed
    finally:
        held.release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        await writer.aclose(timeout=2)


@pytest.mark.asyncio
async def test_fifo_payloads_are_frozen_before_callers_mutate_nested_data(tmp_path, monkeypatch):
    path = tmp_path / "room" / "trace.jsonl"
    held = HeldIO(path, "write", monkeypatch)
    writer = TraceWriter(path)
    await writer.start()
    first = Event("first", {"items": [{"name": "original"}], "path": Path("receipt.txt")}, ts=11)
    second = Event("second", {"values": [2]}, ts=12)
    try:
        writer(first)
        await held.witness_responsive_loop()
        first.data["items"][0]["name"] = "changed"
        first.data["items"].append({"name": "later"})
        first.type, first.ts = "replaced", 99
        writer(second)
        second.data["values"].append(200)
        writer(Event("third", {"value": 3}, ts=13))
        held.release.set()
        assert await writer.aclose(timeout=2)
        assert records(path) == [
            {"type": "first", "data": {"items": [{"name": "original"}], "path": "receipt.txt"}, "ts": 11},
            {"type": "second", "data": {"values": [2]}, "ts": 12},
            {"type": "third", "data": {"value": 3}, "ts": 13},
        ]
        assert (writer.accepted, writer.written, writer.rejected) == (3, 3, 0)
    finally:
        held.release.set()
        await writer.aclose(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["write", "flush"])
async def test_uncertain_persistence_is_reported_without_retry_or_later_append(tmp_path, monkeypatch, caplog, failure):
    path = tmp_path / "room" / "trace.jsonl"
    held = HeldIO(path, failure, monkeypatch, fail=failure)
    writer = TraceWriter(path)
    await writer.start()
    first = Event("first", {"value": 1}, ts=1)
    first_line = json.dumps(first.to_dict(), default=str) + "\n"
    failure_reported = asyncio.Event()

    class FailureSignal(logging.Handler):
        def emit(self, record):
            if record.exc_info:
                failure_reported.set()

    signal = FailureSignal()
    logger = logging.getLogger("prism.trace")
    logger.addHandler(signal)
    try:
        writer(first)
        await held.witness_responsive_loop()
        writer(Event("queued-before-failure", {"value": 2}, ts=2))
        held.release.set()
        await asyncio.wait_for(failure_reported.wait(), 2)
        # Still open: rejection must be due to failed persistence, not closing.
        writer(Event("submitted-after-failure", {"value": 3}, ts=3))
        assert not await writer.aclose(timeout=2)
        assert not await writer.aclose(timeout=2)
        assert (writer.accepted, writer.written, writer.rejected) == (2, 0, 1)
        assert sum(phase == "write" for phase, _ in held.calls) == 1
        assert path.read_text() == (first_line[:len(first_line) // 2] if failure == "write" else first_line)
        assert "injected" in caplog.text and "trace I/O failed" in caplog.text
        assert held.file.closed
    finally:
        held.release.set()
        await writer.aclose(timeout=2)
        logger.removeHandler(signal)


@pytest.mark.asyncio
async def test_close_timeout_and_cancelled_waiter_preserve_eventual_drain(tmp_path, monkeypatch):
    path = tmp_path / "room" / "trace.jsonl"
    held = HeldIO(path, "write", monkeypatch)
    writer = TraceWriter(path)
    await writer.start()
    waiter = None
    try:
        writer(Event("first", {}, ts=1))
        await held.witness_responsive_loop()
        writer(Event("second", {}, ts=2))
        assert not await writer.aclose(timeout=0.01)
        assert not held.guard_released.is_set() and not held.file.closed
        waiter = asyncio.create_task(writer.aclose(timeout=2))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert writer.written == 0 and not held.file.closed
        held.release.set()
        assert await writer.aclose(timeout=2)
        assert [row["type"] for row in records(path)] == ["first", "second"]
        assert (writer.accepted, writer.written, writer.rejected) == (2, 2, 0)
        assert sum(phase == "close" for phase, _ in held.calls) == 1
    finally:
        held.release.set()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        await writer.aclose(timeout=2)


@pytest.mark.asyncio
async def test_blocked_room_does_not_hold_another_rooms_trace(tmp_path, monkeypatch):
    slow_path, healthy_path = tmp_path / "slow" / "trace.jsonl", tmp_path / "healthy" / "trace.jsonl"
    held = HeldIO(slow_path, "write", monkeypatch)
    slow, healthy = TraceWriter(slow_path), TraceWriter(healthy_path)
    await slow.start()
    try:
        slow(Event("slow-room", {}, ts=1))
        await held.witness_responsive_loop()
        await healthy.start()
        healthy(Event("healthy-room", {}, ts=2))
        assert await healthy.aclose(timeout=1)
        assert not held.guard_released.is_set() and not held.release.is_set()
        assert slow.written == 0
        assert records(healthy_path) == [{"type": "healthy-room", "data": {}, "ts": 2}]
        held.release.set()
        assert await slow.aclose(timeout=2)
        assert [row["type"] for row in records(slow_path)] == ["slow-room"]
    finally:
        held.release.set()
        await asyncio.gather(slow.aclose(timeout=2), healthy.aclose(timeout=2))


@pytest.mark.asyncio
async def test_open_failure_is_observable_and_rejects_events(tmp_path, monkeypatch, caplog):
    path = tmp_path / "room" / "trace.jsonl"
    original_open = Path.open

    def denied(path_to_open, *args, **kwargs):
        if path_to_open == path:
            raise PermissionError("injected trace open failure")
        return original_open(path_to_open, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    writer = TraceWriter(path)
    try:
        with pytest.raises(PermissionError, match="injected trace open failure"):
            await writer.start()
        writer(Event("not-persisted", {}, ts=1))
        assert not await writer.aclose(timeout=2)
        assert (writer.accepted, writer.written, writer.rejected) == (0, 0, 1)
        assert "injected trace open failure" in caplog.text
        assert not path.exists()
    finally:
        await writer.aclose(timeout=2)


@pytest.mark.asyncio
async def test_event_bus_serialization_rejection_cannot_report_complete_trace(tmp_path, caplog):
    path = tmp_path / "room" / "trace.jsonl"
    writer = TraceWriter(path)
    bus = EventBus()
    await writer.start()
    bus.subscribe(writer)
    try:
        kept = bus.emit("valid-before-rejection", value=7)
        bus.emit("invalid-json-key", payload={("tuple", "key"): "not JSON"})
        assert len(bus.history) == 2
        assert not await writer.aclose(timeout=2)
        assert records(path) == [kept.to_dict()]
        assert (writer.accepted, writer.written, writer.rejected) == (1, 1, 1)
        assert "trace event submission failed" in caplog.text
        assert "complete=False" in caplog.text
    finally:
        await writer.aclose(timeout=2)
