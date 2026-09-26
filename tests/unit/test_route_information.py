"""Named route-information contract; scripted models, real tools, no audio claims.

Unknown/ambiguous places, qualified/compound requests, current-route questions,
and nondefault units deliberately remain outside this narrow contract.
"""
from copy import deepcopy
import json

import pytest

from agent.coordinator import events as E
from agent.coordinator.resolver import IntentResolver, PlanContractError
from agent.tools import car_tools
from tests.unit.test_navigation_clarification import build, lookup
from tests.unit.test_navigation_plan_contract import Model, plan


TARGET = "MG Road Metro Station"
QUESTION = f"How far is {TARGET}?"
NO_CALLS = {"complete": True, "calls": [], "reply": "Okay."}


def route(destination=TARGET, **kwargs):
    return plan(destination, activate=False, **kwargs)


@pytest.mark.parametrize("question,destination", [
    (QUESTION, TARGET),
    ("Please how far to Cubbon Park from here?", "Cubbon Park"),
    ("How far away is Whitefield (ITPL)?", "Whitefield (ITPL)"),
    ("What is the distance to office?", "office"),
    ("What's the distance to airport?", "airport"),
    ("How long does it take to drive to Cubbon Park?", "Cubbon Park"),
    ("How long would it take to get to office?", "office"),
    ("How long is the drive to Samsung?", "Samsung"),
    ("What is the driving time to MTR restaurant, Lalbagh Road?", "MTR restaurant, Lalbagh Road"),
    ("What's the driving time to MG Road Metro Station.", TARGET),
])
async def test_complete_information_question_rejects_lookup_only(question, destination):
    original = lookup(destination)
    resolver = IntentResolver(Model([original]), car_tools.build_car_manifest())
    with pytest.raises(PlanContractError) as caught:
        await resolver.resolve(question, [], {}, [])
    assert caught.value.rejected_plan == original
    assert "compute_route" in caught.value.reason


@pytest.mark.parametrize("destination,search,via", [
    (TARGET, False, None), ("P_MGROAD", False, ""),
    ("$lookup.places[0].place_id", True, None),
    ("$lookup.places[0].name", True, ""),
])
async def test_matching_route_plan_preserves_refs_and_optional_empty_via(destination, search, via):
    candidate = route(search=search)
    candidate["calls"][-1]["args"].update(destination=destination, via=via)
    if search:
        candidate["calls"].reverse()  # Dependency graph, not array order, controls execution.
    result = await IntentResolver(Model([candidate]), car_tools.build_car_manifest()).resolve(
        QUESTION, [], {}, [])
    assert [c.__dict__ for c in result.calls] == candidate["calls"]


@pytest.mark.parametrize("bad", [
    NO_CALLS,
    {**route(), "complete": False},
    route("Cubbon Park"),
    {"calls": [*route()["calls"], {"id": "other", "tool": "compute_route", "args": {"destination": TARGET}}]},
    {"calls": [{"id": "route", "tool": "compute_route", "args": {"destination": TARGET, "via": "Cubbon Park"}}]},
    {"calls": [*route()["calls"], {"id": "lookup", "tool": "search_destination", "args": {"query": "airport"}}]},
    {"calls": [*route()["calls"], {"id": "extra", "tool": "find_nearby", "args": {"category": "coffee"}}]},
    {"calls": [*route()["calls"], {"id": "write", "tool": "start_navigation", "args": {"route_id": "$route.route_id"}}]},
    {"calls": [*route()["calls"], {"id": "write", "tool": "cancel_navigation", "args": {}}]},
    {"calls": [*route()["calls"], {"id": "write", "tool": "add_waypoint", "args": {"place_id": "K_BLUETOKAI"}}]},
])
async def test_invalid_whole_information_plan_has_one_repair_and_zero_dispatch(build, bad):
    coordinator, model, bus, logged, spoken = build([bad, bad])
    try:
        coordinator.on_user_turn(QUESTION)
        await coordinator.drain()
        assert [e.data["attempt"] for e in bus.of_type(E.PLAN_REJECTED)] == [1, 2]
        assert len(model.requests) == 2
        assert not logged and not bus.of_type(E.TOOL_STARTED)
        assert spoken == ["Sorry, could you say that again?"]
    finally:
        await coordinator.aclose()


@pytest.mark.parametrize("question", [
    "How far is Unmapped Observatory?", "How far is Airport Road?",
    "How far is MG Road Metro Station from office?",
    "How far is MG Road Metro Station in miles?",
    "How long does it take to walk to MG Road Metro Station?",
    "How far is MG Road Metro Station via Cubbon Park?",
    "How long does it take to drive to MG Road Metro Station tomorrow?",
    "How far is it?", "What is my ETA?", "How long is left on the current route?",
    "How far is MG Road Metro Station and what time does it close?",
    "Navigate to MG Road Metro Station and tell me the distance.",
    "Don't tell me how far MG Road Metro Station is.",
])
async def test_outside_contract_preserves_ordinary_planner_behavior(question):
    candidate = lookup(TARGET)
    candidate["repaired"] = QUESTION  # Model rewriting cannot enlarge the contract.
    model = Model([candidate])
    resolution = await IntentResolver(model, car_tools.build_car_manifest()).resolve(question, [], {}, [])
    assert [c.__dict__ for c in resolution.calls] == candidate["calls"]
    assert len(model.requests) == 1


async def test_repaired_read_supplies_actual_result_and_original_question_to_summary(build):
    coordinator, model, bus, logged, _ = build([lookup(TARGET), route(search=True)])
    try:
        coordinator.on_user_turn(QUESTION)
        await coordinator.drain()
        assert len(model.requests) == 2 and len(bus.of_type(E.PLAN_REJECTED)) == 1
        assert [name for name, _ in logged] == ["search_destination", "compute_route"]
        assert all(QUESTION in prompt for _, prompt in model.requests)
        done = next(e.data["result"] for e in bus.of_type(E.TOOL_DONE) if e.data["tool"] == "compute_route")
        assert done["destination_id"] == "P_MGROAD" and done["distance_km"] > 0 and done["eta_min"] > 0
        summary = model.summary_requests[-1]
        assert f"User request: {QUESTION}" in summary
        outcomes = json.loads(summary.split("\nResults: ", 1)[1])
        assert next(o["result"] for o in outcomes if o["tool"] == "compute_route") == done
        assert bus.of_type(E.PLAN_READY)[-1].data["continuation"] is None
    finally:
        await coordinator.aclose()


async def test_information_read_does_not_replace_existing_route_or_stops(build):
    stop = {"calls": [{"id": "stop", "tool": "add_waypoint", "args": {"place_id": "K_BLUETOKAI"}}]}
    coordinator, _, bus, logged, _ = build([plan("airport"), stop, route()])
    try:
        for utterance in ("Navigate to airport.", "Add Blue Tokai, Indiranagar as a stop."):
            coordinator.on_user_turn(utterance)
            await coordinator.drain()
        before = deepcopy(next(e.data["result"] for e in bus.of_type(E.TOOL_DONE) if e.data["tool"] == "add_waypoint"))
        offset = len(logged)
        coordinator.on_user_turn(QUESTION)
        await coordinator.drain()
        assert [name for name, _ in logged[offset:]] == ["compute_route"]
        # Public backend no-op on the existing route exposes its current snapshot.
        after = await coordinator.executor.manifest.get("start_navigation").fn(before["route_id"])
        assert after["already_active"] is True
        for key in ("route_active", "route_id", "destination_id", "navigation_version", "stop_ids", "eta_min", "distance_km"):
            assert after[key] == before[key]
    finally:
        await coordinator.aclose()


async def test_question_and_cancel_consume_old_navigation_clarification(build):
    cancel = {"calls": [{"id": "cancel", "tool": "cancel_navigation", "args": {}}]}
    coordinator, model, bus, logged, _ = build([plan("Airport Road", search=True), route(), cancel, lookup(TARGET)])
    try:
        for utterance in ("Navigate to Airport Road.", QUESTION, "Stop navigation.", f"I mean {TARGET}."):
            coordinator.on_user_turn(utterance)
            await coordinator.drain()
        assert all(e.data["continuation"] is None for e in bus.of_type(E.PLAN_READY))
        assert len(model.requests) == 4
        assert all(name not in {"start_navigation", "add_waypoint"} for name, _ in logged)
        result = next(e.data["result"] for e in bus.of_type(E.TOOL_DONE) if e.data["tool"] == "cancel_navigation")
        assert result["route_active"] is False and result["navigation_version"] == 0
        assert result["cancelled"] is None
    finally:
        await coordinator.aclose()


async def test_information_read_reuses_same_context_but_not_after_navigation_change(build):
    coordinator, _, bus, logged, _ = build([route(), route(), plan("office"), route()])
    try:
        for utterance in (QUESTION, QUESTION, "Navigate to office.", QUESTION):
            coordinator.on_user_turn(utterance)
            await coordinator.drain()
        target_reads = [(name, args) for name, args in logged if name == "compute_route" and args["destination"] == TARGET]
        assert len(target_reads) == 2
        reused = [e for e in bus.of_type(E.TOOL_REUSED) if e.data["tool"] == "compute_route"]
        assert len(reused) == 1
        assert len([e for e in bus.of_type(E.TOOL_DONE) if e.data["tool"] == "start_navigation"]) == 1
    finally:
        await coordinator.aclose()


async def test_correction_at_information_rejection_prevents_old_repair_or_dispatch(build):
    cancel = {"calls": [{"id": "cancel", "tool": "cancel_navigation", "args": {}}]}
    coordinator, model, bus, logged, _ = build([lookup(TARGET), cancel])

    def correct(event):
        if event.type == E.PLAN_REJECTED:
            coordinator.on_user_turn("Stop navigation.")

    bus.subscribe(correct)
    try:
        coordinator.on_user_turn(QUESTION)
        await coordinator.drain()
        assert len(bus.of_type(E.PLAN_REJECTED)) == 1 and len(model.requests) == 2
        assert [name for name, _ in logged] == ["cancel_navigation"]
        summary = model.summary_requests[-1]
        assert summary.startswith(f"User request: {QUESTION} Stop navigation.\nResults: ")
        outcomes = json.loads(summary.split("\nResults: ", 1)[1])
        assert [outcome["tool"] for outcome in outcomes] == ["cancel_navigation"]
        assert outcomes[0]["status"] == "done" and outcomes[0]["result"]["cancelled"] is None
    finally:
        await coordinator.aclose()
