"""Original details survive resolver repair before result synthesis.

Scripted repair isolates coordinator/responder wiring. Real navigation tools
provide the outcomes; the recording LLM performs no inference.
"""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agent.coordinator import events as E
from agent.coordinator.coordinator import Coordinator, CoordinatorConfig
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.coordinator.resolver import Resolution
from agent.coordinator.responder import Responder
from agent.tools import car_tools


class RecordingLLM:
    response = "The requested result details reached summary synthesis."

    def __init__(self):
        self.requests = []

    async def complete(self, system, user):
        self.requests.append((system, user))
        return self.response


class ScriptedResolver:
    def __init__(self, repaired, calls):
        self.resolution = Resolution(True, repaired, [], {}, calls, "Preparing the request.")
        self.seen = []

    async def resolve(self, utterance, history, slots, completed):
        self.seen.append(utterance)
        return self.resolution


def route_calls(destination="MG Road Metro Station"):
    return [PlannedCall("find", "search_destination", {"query": destination}),
            PlannedCall("route", "compute_route", {"destination": "$find.places[0].place_id"}),
            PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"})]


@pytest_asyncio.fixture
async def conversation(monkeypatch):
    monkeypatch.setattr(car_tools, "ROUTE_COMPUTE_S", 0)
    coordinators = []

    def make(repaired, calls):
        bus, ledger = EventBus(), Ledger()
        engine = Executor(car_tools.build_car_manifest(), ledger, bus)
        resolver, llm, said = ScriptedResolver(repaired, calls), RecordingLLM(), []

        async def speak(text):
            said.append(text)

        coord = Coordinator(resolver, engine, ledger, bus, speak, Responder(llm),
                            CoordinatorConfig(hold_read_ms=0, hold_state_ms=0,
                                              hold_incomplete_ms=0, ack_after_ms=60_000))
        coordinators.append(coord)
        return SimpleNamespace(coord=coord, engine=engine, bus=bus, resolver=resolver,
                               llm=llm, said=said)

    yield make
    for coord in coordinators:
        await coord.aclose()


def summary_payload(room, original, repaired):
    assert len(room.llm.requests) == 1, "repair must not bypass synthesis of requested details"
    payload = room.llm.requests[0][1]
    assert original in payload, "fallback lost the original requested details"
    if repaired:
        assert repaired in payload, "fallback lost the repaired action context"
    assert room.said == [room.llm.response]
    assert room.resolver.seen == [original]
    assert not room.bus.of_type(E.TOOL_ERROR)
    return json.loads(payload.split("\nResults: ", 1)[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [
    "Navigate to MG Road Metro Station.",
    "Summarize the route to MG Road Metro Station.",
])
async def test_repaired_distance_request_retains_original_and_corrected_context(conversation, repaired):
    original = ("Navigate to Cubbon Park, actually MG Road Metro Station instead, "
                "and tell me the route distance in kilometers.")
    room = conversation(repaired, route_calls())
    room.coord.on_user_turn(original)
    await room.coord.drain(timeout=1)
    action = [e.data for e in room.bus.of_type(E.TOOL_DONE) if e.data["tool"] == "start_navigation"]
    assert len(action) == 1 and action[0]["result"]["destination_id"] == "P_MGROAD"
    outcomes = summary_payload(room, original, repaired)
    route = next(o["result"] for o in outcomes if o["tool"] == "compute_route")
    assert route["distance_km"] == action[0]["result"]["distance_km"]
    assert route["destination"] == "MG Road Metro Station"


@pytest.mark.asyncio
async def test_repaired_list_request_keeps_all_places_needed_for_requested_answer(conversation):
    original = "List all nearby coffee places and add the nearest one as a stop."
    repaired = "Add the nearest coffee stop to the route."
    room = conversation(repaired, [
        PlannedCall("nearby", "find_nearby", {"category": "coffee"}),
        PlannedCall("stop", "add_waypoint", {"place_id": "$nearby.places[0].place_id"}),
    ])
    await room.engine.run(route_calls(), operation_id="setup")
    room.coord.on_user_turn(original)
    await room.coord.drain(timeout=1)
    actions = [e.data for e in room.bus.of_type(E.TOOL_DONE) if e.data["tool"] == "add_waypoint"]
    assert len(actions) == 1 and actions[0]["result"]["already_present"] is False
    outcomes = summary_payload(room, original, repaired)
    places = next(o["result"]["places"] for o in outcomes if o["tool"] == "find_nearby")
    assert len(places) == 3 and len({p["name"] for p in places}) == 3
    assert actions[0]["result"]["added"] == places[0]["name"]


@pytest.mark.asyncio
async def test_empty_repair_keeps_original_detail_request(conversation):
    original = "Navigate to MG Road Metro Station and tell me the route distance."
    room = conversation("", route_calls())
    room.coord.on_user_turn(original)
    await room.coord.drain(timeout=1)
    assert len(summary_payload(room, original, "")) == 3


@pytest.mark.asyncio
async def test_recognized_pure_command_with_benign_repair_stays_direct(conversation):
    original = "Please navigate to MG Road Metro Station."
    room = conversation("Navigate to MG Road Metro Station.", route_calls())
    room.coord.on_user_turn(original)
    await room.coord.drain(timeout=1)
    assert not room.llm.requests
    assert len(room.said) == 1
    result = next(e.data["result"] for e in room.bus.of_type(E.TOOL_DONE)
                  if e.data["tool"] == "start_navigation")
    assert result["destination"] in room.said[0] and str(result["eta_min"]) in room.said[0]
    assert room.resolver.seen == [original]
