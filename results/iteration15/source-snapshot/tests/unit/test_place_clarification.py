"""Independent development cases for the fixed demo map, not held-out audio."""

from __future__ import annotations

import pytest

from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.coordinator.responder import Responder
from agent.tools.car_tools import PLACES, build_car_manifest


@pytest.fixture
def car(monkeypatch):
    monkeypatch.setattr("agent.tools.car_tools.ROUTE_COMPUTE_S", 0)
    return build_car_manifest()


KNOWN_QUERIES = [(p["place_id"], p["place_id"]) for p in PLACES] + [
    (p["name"], p["place_id"]) for p in PLACES
] + [
    ("work", "P_OFFICE"), ("office", "P_OFFICE"), ("home", "P_HOME"),
    ("airport", "P_AIRPORT"), ("samsung", "P_SRIB"), ("hospital", "P_HOSP"),
    ("Blue Tokai", "K_BLUETOKAI"), ("Starbucks", "K_SBUX_AIR"),
    ("MG Road", "P_MGROAD"), ("Manyata Tech Park", "P_OFFICE"),
    ("Third Wave Coffee", "K_TWC_HEB"), ("take me to airport", "P_AIRPORT"),
    ("take me to Starbucks Airport Road", "K_SBUX_AIR"),
]


@pytest.mark.parametrize("query,expected_id", KNOWN_QUERIES)
async def test_known_and_unique_partial_places_remain_usable(car, query, expected_id):
    route = await car.get("compute_route").fn(destination=query)
    assert route["status"] == "success"
    assert route["destination_id"] == expected_id
    started = await car.get("start_navigation").fn(route_id=route["route_id"])
    assert started["destination_id"] == expected_id


@pytest.mark.parametrize("query", [
    "airport hotel", "Columbia Asia Hospital", "Blue Tokai Koramangala",
    "Dabolim International Airport", "my office in Whitefield", "Cubbon Park Annex",
])
async def test_unknown_qualified_places_never_replace_active_destination(car, query):
    route = await car.get("compute_route").fn(destination="office")
    before = await car.get("start_navigation").fn(route_id=route["route_id"])
    result = await car.get("compute_route").fn(destination=query)
    assert result["status"] == "error"
    assert result["code"] == "place_not_found"
    assert "route_id" not in result
    observed = await car.get("start_navigation").fn(route_id=route["route_id"])
    assert observed["destination_id"] == before["destination_id"]
    assert observed["navigation_version"] == before["navigation_version"]


@pytest.mark.parametrize("query,candidate_ids", [
    ("Airport Road", {"P_HOSP", "K_SBUX_AIR"}),
    ("Tech Park", {"P_OFFICE", "P_SRIB"}),
    ("petrol pump", {"F_IOC_HEB", "F_HP_KR"}),
    ("Indiranagar", {"P_HOME", "C_ATHER_IND", "K_BLUETOKAI"}),
    ("coffee", {"K_TWC_HEB", "K_BLUETOKAI", "K_SBUX_AIR"}),
])
async def test_ambiguous_search_reports_candidates_without_selecting_one(car, query, candidate_ids):
    result = await car.get("search_destination").fn(query=query)
    assert result["status"] == "error"
    assert result["code"] == "place_ambiguous"
    assert result["places"] == []
    assert {p["place_id"] for p in result["candidates"]} == candidate_ids
    route = await car.get("compute_route").fn(destination=query)
    assert route["status"] == "error"
    assert route["code"] == "place_ambiguous"
    assert "route_id" not in route


@pytest.mark.parametrize("stop", ["Airport Road", "airport hotel"])
async def test_uncertain_waypoint_or_via_preserves_route_and_version(car, stop):
    route = await car.get("compute_route").fn(destination="office")
    before = await car.get("start_navigation").fn(route_id=route["route_id"])
    waypoint = await car.get("add_waypoint").fn(place_id=stop)
    assert waypoint["status"] == "error"
    via = await car.get("compute_route").fn(destination="airport", via=stop)
    assert via["status"] == "error"
    after = await car.get("start_navigation").fn(route_id=route["route_id"])
    assert after["stop_ids"] == before["stop_ids"] == []
    assert after["polyline"] == before["polyline"]
    assert after["navigation_version"] == before["navigation_version"]


@pytest.mark.parametrize("query,code", [
    ("Airport Road", "place_ambiguous"), ("Cubbon Prak", "place_not_found"),
])
async def test_uncertain_chained_search_does_not_dispatch_navigation(car, query, code):
    logged = []
    executor = Executor(car, Ledger(), EventBus(), lambda tool, *args: logged.append(tool))
    result = await executor.run([
        PlannedCall("search", "search_destination", {"query": query}),
        PlannedCall("route", "compute_route", {"destination": "$search.places[0].place_id"}),
        PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"}),
    ])
    assert result.outcomes["search"].status == "error"
    assert result.outcomes["search"].result["code"] == code
    assert result.outcomes["route"].status == result.outcomes["start"].status == "skipped"
    assert logged == ["search_destination"]
    assert (await car.get("cancel_navigation").fn())["cancelled"] is None


@pytest.mark.parametrize("query,expected_name", [
    ("Airport Road", "Starbucks, Airport Road"),
    ("Cubbon Prak", "Cubbon Park"),
    ("NONEXISTENT_ZZZ_1289", None),
])
async def test_clarification_is_short_grounded_and_independent_of_model(car, query, expected_name):
    class UnavailableLLM:
        async def complete(self, *args):
            pytest.fail("Place clarification must not require a model round trip")

    executor = Executor(car, Ledger(), EventBus(), lambda *args: None)
    result = await executor.run([PlannedCall("route", "compute_route", {"destination": query})])
    text = await Responder(UnavailableLLM()).summarize(f"take me to {query}", result)
    assert text
    assert len(text.split()) <= 30
    assert "?" in text or "Please" in text
    if expected_name:
        assert expected_name in text
    else:
        assert "couldn't find" in text


async def test_explicit_clarification_can_complete_original_destination_request(car):
    executor = Executor(car, Ledger(), EventBus(), lambda *args: None)
    first = await executor.run([
        PlannedCall("route", "compute_route", {"destination": "Airport Road"}),
        PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"}),
    ])
    assert first.outcomes["route"].status == "error"
    clarified = await executor.run([
        PlannedCall("route", "compute_route", {"destination": "Starbucks, Airport Road"}),
        PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"}),
    ])
    assert clarified.outcomes["start"].status == "done"
    assert clarified.outcomes["start"].result["destination_id"] == "K_SBUX_AIR"
    assert (await car.get("cancel_navigation").fn())["cancelled"] == "Starbucks, Airport Road"


@pytest.mark.parametrize("query,place_id", [
    ("Blue Tokai in Indiranagar", "K_BLUETOKAI"),
    ("Office at Manyata Tech Park", "P_OFFICE"),
    ("Ather Grid charger in Indiranagar", "C_ATHER_IND"),
    ("Third Wave Coffee at Hebbal", "K_TWC_HEB"),
    ("Starbucks at Airport Road", "K_SBUX_AIR"),
    ("Home in Indiranagar", "P_HOME"),
])
async def test_spoken_connectives_match_one_complete_stored_name(car, query, place_id):
    found = await car.get("search_destination").fn(query=query)
    assert found["status"] == "success"
    assert found["places"][0]["place_id"] == place_id


@pytest.mark.parametrize("query", [
    "airport in Delhi", "Columbia Asia Hospital in Indiranagar", "Blue Tokai in Koramangala",
    "Airport in Road", "Blue Tokai in", "in Blue Tokai Indiranagar",
])
async def test_connectives_do_not_enable_fuzzy_alias_or_partial_selection(car, query):
    result = await car.get("compute_route").fn(destination=query)
    assert result["status"] == "error"
    assert "route_id" not in result


async def test_connective_normalization_with_two_full_matches_still_clarifies(car, monkeypatch):
    import agent.tools.car_tools as car_tools
    duplicate = {**next(p for p in PLACES if p["place_id"] == "K_BLUETOKAI"),
                 "place_id": "K_BLUETOKAI_SECOND", "name": "Blue Tokai (Indiranagar)"}
    monkeypatch.setattr(car_tools, "PLACES", [*PLACES, duplicate])
    result = await car.get("search_destination").fn(query="Blue Tokai in Indiranagar")
    assert result["status"] == "error"
    assert result["code"] == "place_ambiguous"
    assert {p["place_id"] for p in result["candidates"]} == {"K_BLUETOKAI", "K_BLUETOKAI_SECOND"}


async def test_spoken_connective_waypoint_updates_the_actual_route(car):
    route = await car.get("compute_route").fn(destination="office")
    await car.get("start_navigation").fn(route_id=route["route_id"])
    result = await car.get("add_waypoint").fn(place_id="Blue Tokai in Indiranagar")
    assert result["status"] == "success"
    assert result["stop_ids"] == ["K_BLUETOKAI"]
    assert result["destination_id"] == "P_OFFICE"
    assert result["polyline"] == [[12.9719, 77.6412], [12.97, 77.64], [13.0475, 77.62]]
