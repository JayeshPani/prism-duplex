"""Redundant lookups may confirm an action, but cannot hide other results."""
from copy import deepcopy
import re

import pytest

from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.coordinator.responder import Responder
from agent.tools import car_tools


class NoModel:
    async def complete(self, *_args):
        raise AssertionError("confirmed navigation should use its actual result")


class RecordingModel:
    def __init__(self):
        self.requests = []

    async def complete(self, system, user):
        self.requests.append(user)
        return "Full request handled by model."


@pytest.fixture
def navigation(monkeypatch):
    monkeypatch.setattr(car_tools, "ROUTE_COMPUTE_S", 0)
    manifest = car_tools.build_car_manifest()
    return manifest, Executor(manifest, Ledger(), EventBus())


def calls(query="metro"):
    return [PlannedCall("lookup", "search_destination", {"query": query}),
            PlannedCall("route", "compute_route", {"destination": "MG Road Metro Station"}),
            PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"})]


async def fallback(execution, request="Navigate to the metro."):
    model = RecordingModel()
    assert await Responder(model).summarize(request, execution) == "Full request handled by model."
    assert len(model.requests) == 1 and request in model.requests[0]
    return model.requests[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("reused", [False, True])
@pytest.mark.parametrize("reordered", [False, True])
async def test_redundant_lookup_speaks_complete_replacement_without_model(navigation, reused, reordered):
    manifest, engine = navigation
    prior = await manifest.get("compute_route").fn(destination="Cubbon Park")
    await manifest.get("start_navigation").fn(route_id=prior["route_id"])
    if reused:
        await engine.run([PlannedCall("cached", "search_destination", {"query": "metro"})])
    planned = calls()
    if reordered:
        planned = [planned[2], planned[1], planned[0]]
    execution = await engine.run(planned)
    assert execution.outcomes["lookup"].status == ("reused" if reused else "done")
    result = execution.outcomes["start"].result
    text = await Responder(NoModel()).summarize("Navigate to the metro.", execution)
    assert result["destination"] in text and prior["destination"] in text
    assert "replacing" in text and re.search(rf"\b{result['eta_min']} minutes\b", text)
    assert not re.search(rf"\b{result['route_id']}\b", text)
    assert len(text.split()) <= 30


@pytest.mark.asyncio
async def test_each_redundant_search_must_resolve_the_same_destination(navigation):
    _manifest, engine = navigation
    execution = await engine.run(calls() + [
        PlannedCall("second", "search_destination", {"query": "MG Road Metro Station"})])
    assert "MG Road Metro Station" in await Responder(NoModel()).summarize("Go to the metro.", execution)


@pytest.mark.asyncio
async def test_other_destination_search_preserves_both_results(navigation):
    _manifest, engine = navigation
    execution = await engine.run(calls("Cubbon Park"))
    payload = await fallback(execution)
    assert "Cubbon Park" in payload and "MG Road Metro Station" in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("places", [
    [], None, {}, [None], ["MG Road Metro Station"],
    [{"name": "MG Road Metro Station"}],
    [{"place_id": "", "name": "MG Road Metro Station"}],
    [{"place_id": 7, "name": "MG Road Metro Station"}],
    [{"place_id": "P_MGROAD"}],
    [{"place_id": "P_MGROAD", "name": []}],
    [{"place_id": "P_MGROAD", "name": ""}],
    [{"place_id": "P_MGROAD", "name": "MG Road Metro Station"},
     {"place_id": "P_CUBBON", "name": "Cubbon Park"}],
])
async def test_unsupported_search_results_keep_model_fallback(navigation, places):
    _manifest, engine = navigation
    execution = await engine.run(calls())
    execution.outcomes = deepcopy(execution.outcomes)
    execution.outcomes["lookup"].result["places"] = places
    assert '"tool": "search_destination"' in await fallback(execution)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["$route.destination", ["metro"], ""])
async def test_only_literal_nonempty_string_queries_qualify(navigation, query):
    _manifest, engine = navigation
    planned = calls(query) if isinstance(query, str) and query.startswith("$") else calls()
    execution = await engine.run(planned)
    execution.calls[0].args["query"] = query
    assert '"tool": "search_destination"' in await fallback(execution)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    PlannedCall("preview", "compute_route", {"destination": "Cubbon Park"}),
    PlannedCall("nearby", "find_nearby", {"category": "coffee"}),
    PlannedCall("cancel", "cancel_navigation", {}),
])
async def test_other_unused_calls_are_not_silently_discarded(navigation, extra):
    _manifest, engine = navigation
    execution = await engine.run(calls() + [extra])
    payload = await fallback(execution)
    assert f'"tool": "{extra.tool}"' in payload and '"tool": "start_navigation"' in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", [
    "Navigate to the metro and tell me the distance in kilometers.",
    "List the matching metro places and navigate to the metro.",
    "Please take us across town to the metro.",
])
async def test_full_original_information_requests_keep_all_results(navigation, utterance):
    _manifest, engine = navigation
    execution = await engine.run(calls())
    model = RecordingModel()
    await Responder(model).summarize(utterance, execution, repaired_request="Navigate to the metro.")
    assert len(model.requests) == 1
    payload = model.requests[0]
    assert utterance in payload and "Repaired request (interpretation): Navigate to the metro." in payload
    for tool in ("search_destination", "compute_route", "start_navigation"):
        assert f'"tool": "{tool}"' in payload
    assert f'"distance_km": {execution.outcomes["route"].result["distance_km"]}' in payload


@pytest.mark.asyncio
async def test_via_source_destination_does_not_become_the_requested_destination(navigation):
    _manifest, engine = navigation
    execution = await engine.run([
        PlannedCall("via", "compute_route", {"destination": "metro"}),
        PlannedCall("route", "compute_route", {"destination": "Cubbon Park", "via": "$via.destination_id"}),
        PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"}),
    ])
    result = execution.outcomes["start"].result
    assert result["destination"] == "Cubbon Park" and result["stop_ids"] == ["P_MGROAD"]
    payload = await fallback(execution, "Navigate to the metro.")
    assert "Cubbon Park" in payload and "MG Road Metro Station" in payload
    text = await Responder(NoModel()).summarize("Navigate to Cubbon Park via MG Road Metro Station.", execution)
    assert "Cubbon Park" in text and "MG Road Metro Station" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stale", "missing_id", "empty_id", "wrong_id", "inactive", "boolean_eta"])
async def test_redundant_search_does_not_relax_target_snapshot_checks(navigation, change):
    _manifest, engine = navigation
    execution = await engine.run(calls())
    execution.outcomes = deepcopy(execution.outcomes)
    result = execution.outcomes["start"].result
    if change == "stale":
        execution.stale = True
    elif change == "missing_id":
        del result["destination_id"]
    elif change == "empty_id":
        result["destination_id"] = ""
    elif change == "wrong_id":
        result["destination_id"] = "P_CUBBON"
    elif change == "inactive":
        result["route_active"] = False
    else:
        result["eta_min"] = True
    await fallback(execution)


@pytest.mark.asyncio
async def test_lookup_failure_and_unknown_write_preserve_existing_handling(navigation):
    manifest, engine = navigation
    unresolved = await engine.run(calls("Fictional Observatory"))
    assert unresolved.outcomes["lookup"].status == "error"
    assert unresolved.outcomes["start"].status == "done"
    text = await Responder(NoModel()).summarize("Navigate to the metro.", unresolved)
    assert "couldn't find" in text and "has started" not in text

    async def lost_acknowledgment(route_id):
        raise TimeoutError("acknowledgment lost")

    manifest.tools["start_navigation"].fn = lost_acknowledgment
    unknown = await engine.run(calls())
    assert unknown.outcomes["start"].status == "unknown"
    assert await Responder(NoModel()).summarize("Navigate to the metro.", unknown) == (
        "I couldn't confirm whether that action completed, so I haven't retried it.")


@pytest.mark.asyncio
async def test_other_lookup_error_and_blocked_write_keep_model_path(navigation):
    manifest, engine = navigation

    async def unavailable(query):
        return {"status": "error", "message": "directory unavailable"}

    manifest.tools["search_destination"].fn = unavailable
    failed = await engine.run(calls())
    assert "directory unavailable" in await fallback(failed)

    route = await manifest.get("compute_route").fn(destination="P_MGROAD")
    planned = [PlannedCall("start", "start_navigation", {"route_id": route["route_id"]})]
    await engine.run(planned, operation_id="unchanged-retry")
    blocked = await engine.run(planned, operation_id="unchanged-retry")
    assert blocked.outcomes["start"].status == "blocked"
    assert '"status": "blocked"' in await fallback(blocked, "Start navigation.")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,utterance", [
    ("add_waypoint", {"place_id": "P_MGROAD"}, "Add MG Road Metro Station as a stop."),
    ("cancel_navigation", {}, "Cancel navigation."),
])
async def test_unused_search_exception_does_not_extend_to_other_mutations(navigation, tool, args, utterance):
    manifest, engine = navigation
    route = await manifest.get("compute_route").fn(destination="P_MGROAD")
    await manifest.get("start_navigation").fn(route_id=route["route_id"])
    execution = await engine.run([
        PlannedCall("lookup", "search_destination", {"query": "metro"}), PlannedCall("action", tool, args)])
    await fallback(execution, utterance)
