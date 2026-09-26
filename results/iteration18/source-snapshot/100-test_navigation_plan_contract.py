"""Independent navigation contract checks; real executor, no model or audio service."""
import asyncio
from copy import deepcopy
import json

import pytest

from agent.coordinator import events as E
from agent.coordinator.coordinator import Coordinator, CoordinatorConfig
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor
from agent.coordinator.ledger import Ledger
from agent.coordinator.resolver import IntentResolver, PlanContractError
from agent.tools import car_tools


def plan(destination="Cubbon Park", *, activate=True, search=False):
    calls = []
    if search:
        calls.append({"id": "lookup", "tool": "search_destination", "args": {"query": destination}})
    calls.append({"id": "route", "tool": "compute_route",
                  "args": {"destination": "$lookup.places[0].place_id" if search else destination}})
    if activate:
        calls.append({"id": "activate", "tool": "start_navigation", "args": {"route_id": "$route.route_id"}})
    return {"complete": True, "corrections": [], "calls": calls, "reply": "Preparing navigation."}


class Model:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    async def complete_json(self, system, user):
        self.requests.append((system, user))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = await answer()
        return deepcopy(answer)


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setattr(car_tools, "ROUTE_COMPUTE_S", 0)

    def make(answers):
        model, manifest, bus, ledger = Model(answers), car_tools.build_car_manifest(), EventBus(), Ledger()
        logged, spoken = [], []
        executor = Executor(manifest, ledger, bus, lambda tool, args, *_: logged.append((tool, args)))

        async def speak(text):
            spoken.append(text)

        coordinator = Coordinator(IntentResolver(model, manifest), executor, ledger, bus, speak,
            config=CoordinatorConfig(hold_read_ms=0, hold_state_ms=0, hold_incomplete_ms=0,
                                     ack_after_ms=10000, speak_results=False))
        return coordinator, model, bus, logged, spoken
    return make


@pytest.mark.parametrize("utterance", [
    "Navigate to Cubbon Park.", "Please drive to Cubbon Park!", "Take me to the Cubbon Park.",
    "Actually, go to Cubbon Park instead.", "Head to Cubbon Park again.",
    "Start navigation to Cubbon Park.", "Change the destination to Cubbon Park.",
])
async def test_grounded_whole_command_rejects_compute_only(utterance):
    rejected = plan(activate=False)
    resolver = IntentResolver(Model([rejected]), car_tools.build_car_manifest())
    with pytest.raises(PlanContractError) as caught:
        await resolver.resolve(utterance, [], {}, [])
    assert caught.value.rejected_plan == rejected
    assert "start_navigation" in caught.value.reason


@pytest.mark.parametrize("search,reversed_calls", [(False, False), (True, False), (True, True)])
async def test_valid_current_chain_preserves_call_ids_arguments_and_order(search, reversed_calls):
    accepted = plan(search=search)
    if reversed_calls:
        accepted["calls"].reverse()
    result = await IntentResolver(Model([accepted]), car_tools.build_car_manifest()).resolve(
        "Go to Cubbon Park.", [], {}, [])
    assert [c.__dict__ for c in result.calls] == accepted["calls"]


@pytest.mark.parametrize("destination", ["P_CUBBON", "$lookup.places[0].name"])
async def test_documented_destination_id_and_name_reference_remain_valid(destination):
    accepted = plan(search=True)
    accepted["calls"][1]["args"]["destination"] = destination
    result = await IntentResolver(Model([accepted]), car_tools.build_car_manifest()).resolve(
        "Navigate to Cubbon Park.", [], {}, [])
    assert [c.__dict__ for c in result.calls] == accepted["calls"]


@pytest.mark.parametrize("candidate", [
    plan("MG Road Metro Station"),
    {"calls": [{"id": "lookup", "tool": "search_destination", "args": {"query": "Cubbon Park"}}]},
    {"calls": [*plan(activate=False)["calls"],
               {"id": "start", "tool": "start_navigation", "args": {"route_id": "R1"}}]},
    plan("MG Road Metro Station", search=True),
])
async def test_wrong_target_or_historical_route_does_not_satisfy_fresh_navigation(candidate):
    resolver = IntentResolver(Model([candidate]), car_tools.build_car_manifest())
    with pytest.raises(PlanContractError):
        await resolver.resolve("Navigate to Cubbon Park.", [], {}, [])


@pytest.mark.parametrize("extra", [
    {"id": "cancel", "tool": "cancel_navigation", "args": {}},
    {"id": "again", "tool": "start_navigation", "args": {"route_id": "$route.route_id"}},
    {"id": "elsewhere", "tool": "start_navigation", "args": {"route_id": "R9"}},
    {"id": "stop", "tool": "add_waypoint", "args": {"place_id": "MG Road Metro Station"}},
])
async def test_valid_chain_cannot_hide_an_additional_mutation(build, extra):
    candidate = plan()
    candidate["calls"].append(extra)
    coordinator, model, bus, logged, _ = build([candidate, candidate])
    coordinator.on_user_turn("Navigate to Cubbon Park.")
    try:
        await coordinator.drain()
        assert len(bus.of_type(E.PLAN_REJECTED)) == len(model.requests) == 2
        assert not logged and not bus.of_type(E.TOOL_STARTED)
    finally:
        await coordinator.aclose()


@pytest.mark.parametrize("utterance", [
    "Preview a route to Cubbon Park.", "How long would it take to reach Cubbon Park?",
    "Do not navigate to Cubbon Park.", "If I went to Cubbon Park, what would the route be?",
    "Navigate to Cubbon Park?", "Navigate to Cubbon Park and tell me the distance.",
    "Navigate to Cubbon Park; then stop.", "Navigate to", "Navigate to Seaview Dome.",
    "Navigate to charger.", "Navigate to Cubbon Park, actually MG Road Metro Station.",
])
async def test_outside_narrow_contract_never_gains_calls_or_repair(utterance):
    original = plan(activate=False)
    original["repaired"] = "Navigate to Cubbon Park."  # Not evidence of what was requested.
    model = Model([original])
    result = await IntentResolver(model, car_tools.build_car_manifest()).resolve(utterance, [], {}, [])
    assert [c.__dict__ for c in result.calls] == original["calls"]
    assert len(model.requests) == 1


@pytest.mark.parametrize("candidate", [
    {**plan(activate=False), "complete": False},
    {"complete": True, "calls": [], "reply": "Which place do you mean?"},
])
async def test_incomplete_and_no_call_clarifications_remain_unchanged(candidate):
    result = await IntentResolver(Model([candidate]), car_tools.build_car_manifest()).resolve(
        "Navigate to Cubbon Park.", [], {}, [])
    assert result.complete == candidate["complete"]
    assert [c.__dict__ for c in result.calls] == candidate["calls"]


async def test_repair_preserves_full_original_prompt_and_rejected_json_snapshot():
    original = "Navigate to Cubbon Park and tell me its distance."
    history = [{"role": "user", "text": "Keep the distance in kilometres."}]
    slots, completed = {"preference": "kilometres"}, [{"tool": "cancel_navigation", "status": "done"}]
    rejected_data = plan(activate=False)
    rejected_data["repaired"] = "Compute a route."
    error = PlanContractError("missing activation", rejected_data)
    rejected_data["calls"].clear()
    model = Model([plan()])
    resolver = IntentResolver(model, car_tools.build_car_manifest())
    full_prompt = resolver.build_user_prompt(original, history, slots, completed)
    result = await resolver.repair(original, history, slots, completed, error)
    sent = model.requests[0][1]
    assert sent.startswith(full_prompt)
    assert json.dumps(error.rejected_plan, ensure_ascii=False) in sent
    assert len(error.rejected_plan["calls"]) == 1
    assert original in sent and "kilometres" in sent and "cancel_navigation" in sent
    assert [c.tool for c in result.calls] == ["compute_route", "start_navigation"]


async def test_repeat_after_destination_change_repairs_before_any_third_turn_dispatch(build):
    coordinator, model, bus, logged, _ = build([plan(), plan("MG Road Metro Station"), plan(activate=False)])

    async def repaired():
        assert not [e for e in bus.of_type(E.TOOL_STARTED) if e.data["operation_id"] == "turn-3"]
        assert bus.of_type(E.PLAN_REJECTED)[0].data["attempt"] == 1
        return plan(search=True)

    model.answers.append(repaired)
    try:
        for text in ("Navigate to Cubbon Park.", "Navigate to MG Road Metro Station.", "Navigate to Cubbon Park."):
            coordinator.on_user_turn(text)
            await coordinator.drain()
        starts = [e for e in bus.of_type(E.TOOL_DONE) if e.data["tool"] == "start_navigation"]
        assert [e.data["result"]["destination_id"] for e in starts] == ["P_CUBBON", "P_MGROAD", "P_CUBBON"]
        assert starts[-1].data["result"]["replaced"] == "MG Road Metro Station"
        assert len({e.data["result"]["route_id"] for e in starts}) == 3
        assert len(model.requests) == 4
        assert [tool for tool, _ in logged].count("start_navigation") == 3
        rejected = bus.of_type(E.PLAN_REJECTED)[0]
        assert rejected.data["intent_version"] == 3 and rejected.data["utterance"] == "Navigate to Cubbon Park."
        assert rejected.data["rejected_plan"] == plan(activate=False)
    finally:
        await coordinator.aclose()


async def test_second_contract_failure_is_logged_without_third_attempt_or_effects(build):
    first, second = plan(activate=False), plan("MG Road Metro Station")
    coordinator, model, bus, logged, spoken = build([first, second])
    coordinator.on_user_turn("Navigate to Cubbon Park.")
    try:
        await coordinator.drain()
        rejected = bus.of_type(E.PLAN_REJECTED)
        assert [e.data["attempt"] for e in rejected] == [1, 2]
        assert [e.data["rejected_plan"] for e in rejected] == [first, second]
        assert all(e.data["intent_version"] == 1 for e in rejected)
        assert len(model.requests) == 2 and not logged and not bus.of_type(E.TOOL_STARTED)
        assert spoken == ["Sorry, could you say that again?"]
        assert bus.of_type(E.TOOL_ERROR)[-1].data["intent_version"] == 1
    finally:
        await coordinator.aclose()


@pytest.mark.parametrize("second", [RuntimeError("model unavailable"), {"calls": [{"tool": "unknown"}]}])
async def test_repair_transport_or_schema_failure_has_no_effects(build, second):
    coordinator, model, bus, logged, spoken = build([plan(activate=False), second])
    coordinator.on_user_turn("Navigate to Cubbon Park.")
    try:
        await coordinator.drain()
        assert len(model.requests) == 2 and len(bus.of_type(E.PLAN_REJECTED)) == 1
        assert not logged and not bus.of_type(E.TOOL_STARTED)
        assert spoken == ["Sorry, could you say that again?"]
    finally:
        await coordinator.aclose()


async def test_rejection_sink_correction_prevents_even_starting_repair(build):
    coordinator, model, bus, logged, _ = build([plan(activate=False), {"calls": [], "reply": "Cancelled."}])
    repair_calls = []

    async def forbidden_repair(*args):
        repair_calls.append(args)
        raise AssertionError("obsolete repair should not start")

    coordinator.resolver.repair = forbidden_repair
    bus.subscribe(lambda e: coordinator.on_user_turn("Never mind.") if e.type == E.PLAN_REJECTED else None)
    coordinator.on_user_turn("Navigate to Cubbon Park.")
    try:
        await coordinator.drain()
        assert not repair_calls and not logged and not bus.of_type(E.TOOL_STARTED)
        assert not [e for e in bus.of_type(E.PLAN_READY) if e.data["intent_version"] == 1]
    finally:
        await coordinator.aclose()


@pytest.mark.parametrize("ignore_cancellation", [False, True])
async def test_correction_during_repair_never_dispatches_old_plan(build, ignore_cancellation):
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def repair_answer():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            if not ignore_cancellation:
                raise
            await release.wait()
        return plan()

    coordinator, model, bus, logged, _ = build([
        plan(activate=False), repair_answer, {"calls": [], "reply": "Cancelled."}])
    coordinator.on_user_turn("Navigate to Cubbon Park.")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        coordinator.on_user_turn("Never mind.")
        await asyncio.wait_for(cancelled.wait(), 1)
        release.set()
        await coordinator.drain()
        assert not logged and not bus.of_type(E.TOOL_STARTED)
        assert not [e for e in bus.of_type(E.PLAN_READY) if e.data["intent_version"] == 1]
        assert not [e for e in bus.of_type(E.TOOL_ERROR) if e.data.get("intent_version") == 1]
        assert len(model.requests) == 3
    finally:
        release.set()
        await coordinator.aclose()


async def test_event_sink_cannot_mutate_rejected_plan_used_for_repair(build):
    original = plan(activate=False)
    coordinator, model, bus, _, _ = build([original, plan()])

    def mutate(event):
        if event.type == E.PLAN_REJECTED:
            event.data["rejected_plan"]["calls"][0]["args"]["destination"] = "MG Road Metro Station"

    bus.subscribe(mutate)
    coordinator.on_user_turn("Navigate to Cubbon Park.")
    try:
        await coordinator.drain()
        assert json.dumps(original, ensure_ascii=False) in model.requests[-1][1]
    finally:
        await coordinator.aclose()
