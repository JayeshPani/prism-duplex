"""Independent simulated-map scenarios; these are not held-out benchmark items."""

from __future__ import annotations

import pytest

from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.tools.car_tools import build_car_manifest


@pytest.fixture
def car(monkeypatch):
    monkeypatch.setattr("agent.tools.car_tools.ROUTE_COMPUTE_S", 0)
    return build_car_manifest()


async def navigate(manifest, destination):
    route = await manifest.get("compute_route").fn(destination=destination)
    return await manifest.get("start_navigation").fn(route_id=route["route_id"])


def route_plan(destination):
    return [PlannedCall("route", "compute_route", {"destination": destination}),
            PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,place_id", [
    ("Starbucks, Airport Road", "K_SBUX_AIR"),
    ("Starbucks Airport Road", "K_SBUX_AIR"),
    ("Manipal Hospital, Old Airport Road", "P_HOSP"),
    ("take me to Starbucks Airport Road", "K_SBUX_AIR"),
    ("airport", "P_AIRPORT"),
])
async def test_specific_place_wins_over_generic_alias(car, query, place_id):
    result = await car.get("search_destination").fn(query=query)
    assert result["places"][0]["place_id"] == place_id


@pytest.mark.asyncio
async def test_airport_office_airport_returns_to_airport(car):
    logged = []
    executor = Executor(car, Ledger(), EventBus(), lambda tool, *args: logged.append(tool))
    for destination in ("airport", "office", "airport"):
        await executor.run(route_plan(destination))
    # Observe the backend effect, not just an executor success label.
    cancelled = await car.get("cancel_navigation").fn()
    assert cancelled["cancelled"] == "Kempegowda International Airport"
    assert logged.count("start_navigation") == 3


@pytest.mark.asyncio
async def test_nearby_lookup_changes_with_destination(car):
    executor = Executor(car, Ledger(), EventBus(), lambda *args: None)
    query = [PlannedCall("coffee", "find_nearby", {"category": "coffee"})]
    await executor.run(route_plan("airport"))
    first = (await executor.run(query)).outcomes["coffee"].result
    await executor.run(route_plan("office"))
    second = (await executor.run(query)).outcomes["coffee"].result
    detours = lambda result: {p["place_id"]: p["detour_min"] for p in result["places"]}
    assert detours(first)["K_SBUX_AIR"] == 4
    assert detours(second)["K_SBUX_AIR"] == 79


@pytest.mark.asyncio
async def test_adding_a_second_stop_preserves_first_stop_coordinates(car):
    await navigate(car, "office")
    await car.get("add_waypoint").fn(place_id="K_SBUX_AIR")
    second = await car.get("add_waypoint").fn(place_id="K_BLUETOKAI")
    assert second["polyline"] == [[12.9719, 77.6412], [13.1500, 77.6600],
                                  [12.9700, 77.6400], [13.0475, 77.6200]]


@pytest.mark.asyncio
async def test_existing_stop_is_idempotent_by_resolved_place_id(car):
    await navigate(car, "office")
    first = await car.get("add_waypoint").fn(place_id="K_SBUX_AIR")
    repeated = await car.get("add_waypoint").fn(place_id="Starbucks Airport Road")
    assert repeated["polyline"] == first["polyline"]
    assert repeated["stop_ids"] == ["K_SBUX_AIR"]
    assert repeated["navigation_version"] == first["navigation_version"]


@pytest.mark.asyncio
async def test_starting_active_route_preserves_added_stops(car):
    started = await navigate(car, "office")
    with_stop = await car.get("add_waypoint").fn(place_id="K_BLUETOKAI")
    repeated = await car.get("start_navigation").fn(route_id=started["route_id"])
    assert repeated["polyline"] == with_stop["polyline"]
    assert repeated["stop_ids"] == ["K_BLUETOKAI"]
    assert repeated["navigation_version"] == with_stop["navigation_version"]


@pytest.mark.asyncio
async def test_fresh_route_after_waypoint_replaces_stops(car):
    executor = Executor(car, Ledger(), EventBus(), lambda *args: None)
    try:
        first = (await executor.run(route_plan("airport"))).outcomes["start"].result
        with_stop = (await executor.run([
            PlannedCall("stop", "add_waypoint", {"place_id": "K_BLUETOKAI"}),
        ])).outcomes["stop"].result
        assert with_stop["stop_ids"] == ["K_BLUETOKAI"]

        fresh = await executor.run(route_plan("airport"))
        computed = fresh.outcomes["route"].result
        started = fresh.outcomes["start"].result
        assert computed["stop_ids"] == started["stop_ids"] == []
        assert computed["route_id"] == started["route_id"] != first["route_id"]
        assert started["already_active"] is False
        assert started["navigation_version"] == with_stop["navigation_version"] + 1
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_cancel_returns_an_explicit_empty_route_snapshot(car):
    started = await navigate(car, "airport")
    cancelled = await car.get("cancel_navigation").fn()
    assert cancelled["route_active"] is False
    assert cancelled["route_id"] is None
    assert cancelled["destination"] is None
    assert cancelled["eta_min"] is None
    assert cancelled["polyline"] == []
    assert cancelled["stops"] == cancelled["stop_ids"] == []
    assert cancelled["navigation_version"] == started["navigation_version"] + 1
    second = await car.get("cancel_navigation").fn()
    assert second["navigation_version"] == cancelled["navigation_version"]
    assert (await car.get("add_waypoint").fn(place_id="K_BLUETOKAI"))["status"] == "error"


@pytest.mark.asyncio
async def test_route_context_changes_only_with_successful_mutation(car):
    started = await navigate(car, "office")
    assert started["navigation_version"] == 1
    first = await car.get("find_nearby").fn(category="coffee")
    failed = await car.get("add_waypoint").fn(place_id="NONEXISTENT_ZZZ_1289")
    assert failed["status"] == "error"
    assert (await car.get("find_nearby").fn(category="coffee"))["navigation_version"] == 1
    added = await car.get("add_waypoint").fn(place_id="K_SBUX_AIR")
    after = await car.get("find_nearby").fn(category="coffee")
    assert added["navigation_version"] == after["navigation_version"] == 2
    # Detour must describe appending a stop to the actual route including existing stops.
    before_detours = {p["place_id"]: p["detour_min"] for p in first["places"]}
    after_detours = {p["place_id"]: p["detour_min"] for p in after["places"]}
    assert after_detours["K_SBUX_AIR"] == 0
    assert before_detours != after_detours


@pytest.mark.asyncio
async def test_cancel_invalidates_cached_nearby_route_context(car):
    executor = Executor(car, Ledger(), EventBus(), lambda *args: None)
    await executor.run(route_plan("office"))
    query = [PlannedCall("coffee", "find_nearby", {"category": "coffee"})]
    await executor.run(query)
    await executor.run([PlannedCall("cancel", "cancel_navigation", {})])
    actual = (await executor.run(query)).outcomes["coffee"].result
    expected = await build_car_manifest().get("find_nearby").fn(category="coffee")
    assert actual["places"] == expected["places"]


@pytest.mark.asyncio
async def test_unknown_via_is_an_error_not_a_silently_changed_route(car):
    result = await car.get("compute_route").fn(destination="office", via="NONEXISTENT_ZZZ_1289")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_navigation_state_is_per_conversation(car):
    await navigate(car, "airport")
    other = build_car_manifest()
    assert (await other.get("cancel_navigation").fn())["cancelled"] is None
    assert (await car.get("cancel_navigation").fn())["cancelled"] == "Kempegowda International Airport"
