"""Result-driven navigation speech, with real tools and no model inference."""
from copy import deepcopy
import re

import pytest

from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.coordinator.responder import Responder
from agent.tools import car_tools
from agent.tools.manifest import ToolSpec


class UnavailableLLM:
    async def complete(self, *_args):
        raise AssertionError("successful navigation summary must not need a model")


class FallbackLLM:
    def __init__(self):
        self.requests = []

    async def complete(self, system, user):
        self.requests.append((system, user))
        return "Model fallback summary."


@pytest.fixture
def navigation(monkeypatch):
    monkeypatch.setattr(car_tools, "ROUTE_COMPUTE_S", 0)
    manifest = car_tools.build_car_manifest()
    return manifest, Executor(manifest, Ledger(), EventBus())


def route_calls(destination, *, via=None):
    args = {"destination": "$lookup.places[0].place_id"}
    if via is not None:
        args["via"] = via
    return [PlannedCall("lookup", "search_destination", {"query": destination}),
            PlannedCall("route", "compute_route", args),
            PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"})]


async def prepare_route(manifest, destination="P_MGROAD"):
    return await manifest.get("compute_route").fn(destination=destination)


def assert_number(text, value):
    assert re.search(rf"(?<![\w.]){re.escape(str(value))}(?![\w.])", text), text


def assert_short_without_route_id(text, result):
    assert 0 < len(text.split()) <= 30
    assert not re.search(rf"\b{re.escape(result['route_id'])}\b", text)


async def fallback(execution, request="Summarize these requested actions."):
    llm = FallbackLLM()
    text = await Responder(llm).summarize(request, execution)
    assert text == "Model fallback summary."
    assert len(llm.requests) == 1
    return llm.requests[0][1]


@pytest.mark.asyncio
async def test_start_and_replacement_use_successful_destination_and_eta(navigation):
    _manifest, engine = navigation
    responder = Responder(UnavailableLLM())
    first = await engine.run(route_calls("Cubbon Park"))
    result = first.outcomes["start"].result
    text = await responder.summarize("Navigate to the park.", first)
    assert result["destination"] in text
    assert_number(text, result["eta_min"])
    assert_short_without_route_id(text, result)

    changed = await engine.run(route_calls("MG Road Metro Station"))
    result = changed.outcomes["start"].result
    text = await responder.summarize("Change the destination to the metro station.", changed)
    assert result["destination"] in text and result["replaced"] in text
    assert "replac" in text.lower() or "instead" in text.lower()
    assert_number(text, result["eta_min"])
    assert_short_without_route_id(text, result)


@pytest.mark.asyncio
async def test_start_with_requested_via_names_the_stop(navigation):
    _manifest, engine = navigation
    execution = await engine.run(route_calls("Cubbon Park", via="R_MTR"))
    result = execution.outcomes["start"].result
    text = await Responder(UnavailableLLM()).summarize("Navigate via the restaurant.", execution)
    assert result["destination"] in text and result["stops"][0] in text
    assert_number(text, result["eta_min"])
    assert_short_without_route_id(text, result)


@pytest.mark.asyncio
async def test_already_active_preserves_stops_and_uses_current_eta(navigation):
    manifest, engine = navigation
    route = await prepare_route(manifest)
    await manifest.get("start_navigation").fn(route_id=route["route_id"])
    with_stop = await manifest.get("add_waypoint").fn(place_id="K_TWC_HEB")
    assert with_stop["eta_min"] != route["eta_min"]
    execution = await engine.run([PlannedCall("start", "start_navigation", {"route_id": route["route_id"]})])
    result = execution.outcomes["start"].result
    assert result["already_active"] is True
    assert result["stop_ids"] == with_stop["stop_ids"]
    text = await Responder(UnavailableLLM()).summarize("Start this route again.", execution)
    assert "already" in text.lower() and result["destination"] in text
    assert_number(text, with_stop["eta_min"])
    assert "replac" not in text.lower()
    assert_short_without_route_id(text, result)


@pytest.mark.asyncio
async def test_add_and_repeat_stop_report_current_eta_and_extra_minutes(navigation):
    manifest, engine = navigation
    route = await prepare_route(manifest, "P_WHITEFIELD")
    await manifest.get("start_navigation").fn(route_id=route["route_id"])
    planned = [PlannedCall("stop", "add_waypoint", {"place_id": "K_TWC_HEB"})]
    first = await engine.run(planned)
    result = first.outcomes["stop"].result
    assert result["already_present"] is False and result["extra_min"] > 0
    text = await Responder(UnavailableLLM()).summarize("Add Third Wave Coffee in Hebbal.", first)
    assert "added" in text.lower() and result["added"] in text
    assert result["destination"] in text
    assert_number(text, result["eta_min"])
    assert_number(text, result["extra_min"])
    assert_short_without_route_id(text, result)

    repeated = await engine.run(planned)
    current = repeated.outcomes["stop"].result
    assert current["already_present"] is True and current["extra_min"] == 0
    assert current["eta_min"] == result["eta_min"]
    text = await Responder(UnavailableLLM()).summarize("Add that same stop again.", repeated)
    assert "already" in text.lower() and current["added"] in text
    assert_number(text, current["eta_min"])
    assert_short_without_route_id(text, current)


@pytest.mark.asyncio
async def test_cancel_active_and_absent_navigation_have_distinct_truthful_speech(navigation):
    manifest, engine = navigation
    route = await prepare_route(manifest, "P_CUBBON")
    await manifest.get("start_navigation").fn(route_id=route["route_id"])
    planned = [PlannedCall("cancel", "cancel_navigation", {})]
    active = await engine.run(planned)
    text = await Responder(UnavailableLLM()).summarize("Stop navigation.", active)
    assert "cancel" in text.lower() or "stopped" in text.lower()
    assert active.outcomes["cancel"].result["cancelled"] in text
    absent = await engine.run(planned)
    assert absent.outcomes["cancel"].result["cancelled"] is None
    text = await Responder(UnavailableLLM()).summarize("Stop navigation.", absent)
    assert any(phrase in text.lower() for phrase in ("already", "no active", "no navigation", "not navigating", "not active"))
    assert "None" not in text and len(text.split()) <= 30


@pytest.mark.asyncio
async def test_reused_prerequisite_is_accepted_but_unrelated_read_is_not(navigation):
    _manifest, engine = navigation
    await engine.run([PlannedCall("cached", "search_destination", {"query": "Cubbon Park"})])
    execution = await engine.run(route_calls("Cubbon Park"))
    assert execution.outcomes["lookup"].status == "reused"
    assert "Cubbon Park" in await Responder(UnavailableLLM()).summarize("Go to the park.", execution)
    mixed = await engine.run(route_calls("MG Road Metro Station") + [PlannedCall("nearby", "find_nearby", {"category": "food"})])
    payload = await fallback(mixed, "Navigate to the metro and list nearby restaurants.")
    assert '"tool": "start_navigation"' in payload and '"tool": "find_nearby"' in payload


@pytest.mark.asyncio
async def test_compute_only_and_multiple_mutations_keep_the_model_path(navigation):
    manifest, engine = navigation
    preview = await engine.run([PlannedCall("route", "compute_route", {"destination": "P_CUBBON"})])
    assert '"tool": "compute_route"' in await fallback(preview, "Preview a route to the park.")
    route = preview.outcomes["route"].result
    await manifest.get("start_navigation").fn(route_id=route["route_id"])
    mixed = await engine.run([PlannedCall("cancel", "cancel_navigation", {}),
                              PlannedCall("start", "start_navigation", {"route_id": route["route_id"]})])
    assert all(outcome.status == "done" for outcome in mixed.outcomes.values())
    payload = await fallback(mixed, "Cancel navigation, then start the same route again.")
    assert '"tool": "cancel_navigation"' in payload and '"tool": "start_navigation"' in payload


@pytest.mark.asyncio
async def test_blocked_retry_does_not_claim_its_historical_destination_is_current(navigation):
    manifest, engine = navigation
    route = await prepare_route(manifest, "P_CUBBON")
    calls = [PlannedCall("start", "start_navigation", {"route_id": route["route_id"]})]
    await engine.run(calls, operation_id="retryable-request")
    other = await prepare_route(manifest, "P_MGROAD")
    await manifest.get("start_navigation").fn(route_id=other["route_id"])
    retried = await engine.run(calls, operation_id="retryable-request")
    assert retried.outcomes["start"].status == "blocked"
    assert '"status": "blocked"' in await fallback(retried, "Retry the earlier operation.")


@pytest.mark.asyncio
async def test_partial_failure_is_not_hidden_by_successful_navigation(navigation):
    manifest, engine = navigation

    async def fail_lookup():
        return {"status": "error", "error": "availability service is down"}

    manifest.add(ToolSpec("check_availability", "", {}, [], False, fail_lookup))
    execution = await engine.run(route_calls("Cubbon Park") + [PlannedCall("availability", "check_availability", {})])
    assert execution.outcomes["start"].status == "done"
    assert execution.outcomes["availability"].status == "error"
    payload = await fallback(execution, "Navigate to the park and check availability.")
    assert "availability service is down" in payload and '"status": "done"' in payload


@pytest.mark.asyncio
async def test_routing_error_and_skipped_start_preserve_failure_details(navigation):
    manifest, engine = navigation

    async def unavailable_route(destination):
        return {"status": "error", "error": "route service unavailable"}

    manifest.tools["compute_route"].fn = unavailable_route
    execution = await engine.run(route_calls("Cubbon Park"))
    assert execution.outcomes["route"].status == "error"
    assert execution.outcomes["start"].status == "skipped"
    payload = await fallback(execution)
    assert "route service unavailable" in payload and '"status": "skipped"' in payload


@pytest.mark.asyncio
async def test_unknown_write_retains_existing_deterministic_uncertainty(navigation):
    manifest, engine = navigation

    async def lost_acknowledgment(route_id):
        raise TimeoutError("acknowledgment lost")

    manifest.tools["start_navigation"].fn = lost_acknowledgment
    execution = await engine.run(route_calls("Cubbon Park"))
    assert execution.outcomes["start"].status == "unknown"
    assert await Responder(UnavailableLLM()).summarize("Navigate to the park.", execution) == (
        "I couldn't confirm whether that action completed, so I haven't retried it.")


@pytest.mark.asyncio
async def test_success_facts_are_not_recovered_from_request_or_place_catalog(navigation):
    manifest, engine = navigation
    route = await prepare_route(manifest)
    real_start = manifest.tools["start_navigation"].fn

    async def remote_result(route_id):
        result = await real_start(route_id=route_id)
        return {**result, "destination": "Harbor Museum", "navigating_to": "Harbor Museum",
                "destination_id": "REMOTE_MUSEUM", "eta_min": 137}

    manifest.tools["start_navigation"].fn = remote_result
    execution = await engine.run([PlannedCall("start", "start_navigation", {"route_id": route["route_id"]})])
    text = await Responder(UnavailableLLM()).summarize("The request mentions a different park and 4 minutes.", execution)
    assert "Harbor Museum" in text and "MG Road" not in text
    assert_number(text, 137)
    assert_short_without_route_id(text, execution.outcomes["start"].result)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing_destination", "boolean_eta", "inactive_route", "string_flag", "contradictory_destination", "stale", "long_name", "missing_outcome"])
async def test_unsupported_snapshots_and_incomplete_executions_fall_back(navigation, change):
    _manifest, engine = navigation
    execution = await engine.run(route_calls("Cubbon Park"))
    execution.outcomes = deepcopy(execution.outcomes)
    result = execution.outcomes["start"].result
    if change == "missing_destination":
        del result["destination"]
    elif change == "boolean_eta":
        result["eta_min"] = True
    elif change == "inactive_route":
        result["route_active"] = False
    elif change == "string_flag":
        result["already_active"] = "false"
    elif change == "contradictory_destination":
        result["navigating_to"] = "A different destination"
    elif change == "stale":
        execution.stale = True
    elif change == "long_name":
        result["destination"] = result["navigating_to"] = " ".join(["Museum"] * 31)
    else:
        del execution.outcomes["lookup"]
    await fallback(execution)
