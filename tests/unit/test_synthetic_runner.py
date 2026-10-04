"""Synthetic runner resource and report checks; no model or network required."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.coordinator.coordinator import Coordinator
from tests.synthetic import run_synthetic as runner
from tests.unit.test_coordinator import ScriptedLLM, make_manifest


SCENARIO = {"id": "test", "turns": [["first"]],
            "expected": [{"tool": "search", "args": {"x": "first"}}]}
CONFIG = {"coordinator": {"hold_read_ms": 0, "ack_after_ms": 10000}}


def setup_run(monkeypatch, coordinator_type, delay=0):
    instances = []
    llm = ScriptedLLM({"first": {"complete": True, "calls": [
        {"id": "c1", "tool": "search", "args": {"x": "first"}}], "reply": "Searching."}})
    llm.client = SimpleNamespace(close=AsyncMock())

    def build_coordinator(*args):
        coord = coordinator_type(*args)
        instances.append(coord)
        return coord

    monkeypatch.setattr(runner, "build_bench_manifest", lambda _: make_manifest({"search": delay}))
    monkeypatch.setattr(runner, "make_llm", lambda _: llm)
    monkeypatch.setattr(runner, "Coordinator", build_coordinator)
    return llm, instances


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_run_closes_resources_and_preserves_dispatched_calls_and_trace(monkeypatch, timeout):
    class ShortDeadlineCoordinator(Coordinator):
        async def drain(self, timeout=60):
            await super().drain(timeout=.01)

    llm, instances = setup_run(monkeypatch, ShortDeadlineCoordinator, delay=1 if timeout else 0)
    try:
        result = await runner.run_one(SCENARIO, CONFIG, verbose=False)
        assert instances[0]._closed
        llm.client.close.assert_awaited_once()
        assert result["pass"] is not timeout
        assert result["calls"] == [("search", {"x": "first"})]
        events = result["events"]
        assert [e["data"]["tool"] for e in events if e["type"] == "tool_started"] == ["search"]
        assert all("ts" in e and "data" in e for e in events)
        if timeout:
            assert result["why"] == "coordinator timeout"
            assert result["plans"]
            assert any(e["type"] == "tool_cancelled" for e in events)
        else:
            assert any(e["type"] == "tool_done" for e in events)
    finally:
        for coord in instances:
            await coord.aclose()


@pytest.mark.asyncio
async def test_cancelled_run_closes_resources(monkeypatch):
    entered = asyncio.Event()

    class WaitingCoordinator(Coordinator):
        async def drain(self, timeout=60):
            entered.set()
            await asyncio.Event().wait()

    llm, instances = setup_run(monkeypatch, WaitingCoordinator, delay=1)
    task = asyncio.create_task(runner.run_one(SCENARIO, CONFIG, verbose=False))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert instances[0]._closed
        llm.client.close.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for coord in instances:
            await coord.aclose()


@pytest.mark.asyncio
async def test_invalid_coordinator_config_still_closes_model_client(monkeypatch):
    llm, _ = setup_run(monkeypatch, Coordinator)
    with pytest.raises(TypeError):
        await runner.run_one(SCENARIO, {"coordinator": {"invalid": True}}, verbose=False)
    llm.client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", ["0", "-1"])
async def test_nonpositive_concurrency_is_rejected(monkeypatch, capsys, concurrency):
    monkeypatch.setattr(sys, "argv", ["run_synthetic", "-j", concurrency])
    monkeypatch.setattr(runner, "load_config", lambda _: {"profile": "test", "llm": {"model": "test"}})
    with pytest.raises(SystemExit) as error:
        async with asyncio.timeout(.1):
            await runner.main()
    assert error.value.code == 2
    assert "positive" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("only", [None, "missing"])
async def test_empty_scenario_selection_is_rejected(monkeypatch, capsys, tmp_path, only):
    (tmp_path / "scenarios.yaml").write_text("- id: available\n" if only else "[]\n")
    monkeypatch.setattr(runner, "HERE", tmp_path)
    monkeypatch.setattr(runner, "load_config", lambda _: {"profile": "test", "llm": {"model": "test"}})
    monkeypatch.setattr(sys, "argv", ["run_synthetic"] + (["--only", only] if only else []))
    with pytest.raises(SystemExit) as error:
        await runner.main()
    assert error.value.code == 2
    assert "no scenarios" in capsys.readouterr().err.lower()
