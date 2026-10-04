"""Deterministic guards: spelled-id hints, placeholder args, read reuse, ledger resets."""

from __future__ import annotations

import asyncio

import pytest

from agent.coordinator import events as E
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.coordinator.normalize import spelled_runs
from agent.coordinator.resolver import IntentResolver
from agent.tools.car_tools import build_car_manifest
from agent.tools.manifest import Manifest, ToolSpec


@pytest.mark.parametrize("text,joined", [
    ("the id is Q R seven seven one", "QR771"),
    ("number F-4-4-0-2", "F4402"),
    ("it's H as in hotel, 3, 3, 8", "H338"),
    ("the model Z nine", "Z9"),
    ("double four six B", "446B"),
])
def test_spelled_runs_detected(text, joined):
    assert [j for _, j in spelled_runs(text)] == [joined]


@pytest.mark.parametrize("text", ["I want a one bedroom", "two tickets please", "I two", "under 300 dollars"])
def test_ordinary_speech_is_not_spelled(text):
    assert spelled_runs(text) == []


def test_placeholder_required_args_reject_the_whole_plan():
    m = Manifest("t")

    async def fn(**kw):
        return {}

    m.add(ToolSpec("track", "", {"order_id": {"type": "string"}}, ["order_id"], False, fn))
    r = IntentResolver.__new__(IntentResolver)
    r.manifest = m
    with pytest.raises(ValueError, match="missing required detail"):
        r._to_resolution({"calls": [{"id": "c1", "tool": "track", "args": {"order_id": "unknown"}},
                                     {"id": "c2", "tool": "track", "args": {"order_id": "QX12"}}]})


@pytest.mark.asyncio
async def test_identical_read_is_reused_not_reexecuted():
    m = Manifest("t")
    n = {"calls": 0}

    async def search(q):
        n["calls"] += 1
        return {"status": "success", "items": [q]}

    m.add(ToolSpec("search", "", {"q": {"type": "string"}}, ["q"], False, search, cache_context=lambda: "catalog-v1"))
    bus, logged = EventBus(), []
    ex = Executor(m, Ledger(), bus, lambda f, a, t0, t1: logged.append(f))
    await ex.run([PlannedCall("c1", "search", {"q": "desk"})])
    r = await ex.run([PlannedCall("c1", "search", {"q": "desk"})])
    assert n["calls"] == 1 and logged == ["search"]
    assert r.outcomes["c1"].status == "reused" and bus.of_type(E.TOOL_REUSED)


@pytest.mark.asyncio
async def test_car_cancel_makes_navigation_repeatable(monkeypatch):
    monkeypatch.setattr("agent.tools.car_tools.ROUTE_COMPUTE_S", 0)
    m = build_car_manifest()
    bus, ledger, logged = EventBus(), Ledger(), []
    ex = Executor(m, ledger, bus, lambda f, a, t0, t1: logged.append(f))
    nav = [PlannedCall("c1", "compute_route", {"destination": "airport"}),
           PlannedCall("c2", "start_navigation", {"route_id": "$c1.route_id"})]
    first = await ex.run(nav, operation_id="navigate-1")
    again = await ex.run(nav, operation_id="navigate-1")
    # Navigation changes the read context. A whole-plan retry computes a new ID,
    # which cannot replace this operation's immutable original write arguments.
    assert again.outcomes["c1"].result["route_id"] != first.outcomes["c1"].result["route_id"]
    assert again.outcomes["c2"].status == "error"
    assert again.outcomes["c2"].error == "operation call identity was reused with different write arguments"
    replay = await ex.run([
        PlannedCall("c2", "start_navigation", first.outcomes["c2"].args),
    ], operation_id="navigate-1")
    assert replay.outcomes["c2"].status == "blocked"
    assert logged.count("start_navigation") == 1
    cancelled = await ex.run([PlannedCall("c1", "cancel_navigation", {})])
    assert cancelled.outcomes["c1"].result["cancelled"] == "Kempegowda International Airport"
    third = await ex.run(nav)                       # after cancel it is a new action
    assert third.outcomes["c2"].status == "done"
    assert logged.count("start_navigation") == 2
    await ex.aclose()


@pytest.mark.asyncio
async def test_car_destination_change_cancels_inflight_route(monkeypatch):
    monkeypatch.setattr("agent.tools.car_tools.ROUTE_COMPUTE_S", 0.4)
    m = build_car_manifest()
    bus, logged = EventBus(), []
    ex = Executor(m, Ledger(), bus, lambda f, a, t0, t1: logged.append((f, a)))
    first = asyncio.create_task(ex.run([PlannedCall("c1", "compute_route", {"destination": "airport"})]))
    await asyncio.sleep(0.1)
    assert ex.cancel_stale() == 1
    await first
    await ex.run([PlannedCall("c1", "compute_route", {"destination": "office"})])
    assert logged == [("compute_route", {"destination": "airport"}), ("compute_route", {"destination": "office"})]
    assert bus.of_type(E.TOOL_CANCELLED)[0].data["args"] == {"destination": "airport"}
