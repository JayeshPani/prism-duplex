"""Conversation-level clarification regressions; no model or transport claims."""
from copy import deepcopy
from dataclasses import replace

import pytest

from agent.coordinator import events as E
from agent.coordinator.coordinator import Coordinator, CoordinatorConfig
from agent.coordinator.events import EventBus
from agent.coordinator.executor import CallOutcome, Executor
from agent.coordinator.ledger import Ledger
from agent.coordinator.resolver import IntentResolver
from agent.coordinator.responder import Responder
from agent.tools import car_tools
from tests.unit.test_navigation_plan_contract import Model, plan


def lookup(destination):
    return {"complete": True, "calls": [
        {"id": "lookup", "tool": "search_destination", "args": {"query": destination}}],
        "reply": "Checking that place."}


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setattr(car_tools, "ROUTE_COMPUTE_S", 0)

    def make(answers):
        model, bus, ledger = Model(answers), EventBus(), Ledger()
        model.summary_requests = []

        async def summarize(system, user):
            model.summary_requests.append(user)
            return "The lookup is complete."

        model.complete = summarize
        manifest = car_tools.build_car_manifest()
        logged, spoken = [], []
        executor = Executor(manifest, ledger, bus, lambda tool, args, *_: logged.append((tool, deepcopy(args))))

        async def speak(text):
            spoken.append(text)

        coordinator = Coordinator(IntentResolver(model, manifest), executor, ledger, bus, speak,
            responder=Responder(model), config=CoordinatorConfig(hold_read_ms=0, hold_state_ms=0,
            hold_incomplete_ms=0, ack_after_ms=10000))
        return coordinator, model, bus, logged, spoken
    return make


@pytest.mark.parametrize("ambiguous,answer,destination,destination_id", [
    ("Airport Road", "I mean Manipal Hospital, Old Airport Road.",
     "Manipal Hospital, Old Airport Road", "P_HOSP"),
    ("Tech Park", "Office (Manyata Tech Park).", "Office (Manyata Tech Park)", "P_OFFICE"),
    ("Indiranagar", "I mean Blue Tokai, Indiranagar.", "Blue Tokai, Indiranagar", "K_BLUETOKAI"),
    ("Airport Road", "The hospital.", "Manipal Hospital, Old Airport Road", "P_HOSP"),
    ("Airport Road", "I mean Manipal Hospital in Old Airport Road.",
     "Manipal Hospital, Old Airport Road", "P_HOSP"),
])
async def test_destination_answer_keeps_pending_navigation_obligation(build, ambiguous, answer,
                                                                    destination, destination_id):
    coordinator, model, bus, logged, spoken = build([
        plan(ambiguous, search=True), lookup(destination), plan(destination, search=True)])
    try:
        coordinator.on_user_turn(f"Please navigate to {ambiguous}.")
        await coordinator.drain()
        assert not [e for e in bus.of_type(E.TOOL_DONE) if e.data['tool'] == 'start_navigation']
        assert spoken and "Which place" in spoken[-1]
        first_attempts = len(logged)

        coordinator.on_user_turn(answer)
        await coordinator.drain()
        starts = [e for e in bus.of_type(E.TOOL_DONE) if e.data['tool'] == 'start_navigation']
        assert len(starts) == 1
        assert starts[0].data['result']['destination_id'] == destination_id
        assert starts[0].data['result']['route_active'] is True
        assert len(model.requests) == 3
        assert [e.data['attempt'] for e in bus.of_type(E.PLAN_REJECTED)] == [1]
        assert [name for name, _ in logged[first_attempts:]] == [
            'search_destination', 'compute_route', 'start_navigation']
        assert destination in spoken[-1] and 'Estimated arrival' in spoken[-1]
        assert bus.of_type(E.USER_FINAL)[-1].data['text'] == answer
        assert coordinator.history[-2] == {'role': 'user', 'text': answer}
    finally:
        await coordinator.aclose()


@pytest.mark.parametrize("answer", [
    "Never mind.", "Stop navigation.", "Do not go to Manipal Hospital, Old Airport Road.",
    "Find Manipal Hospital, Old Airport Road.", "How far is Manipal Hospital, Old Airport Road?",
    "Manipal Hospital, Old Airport Road?", "The first one.", "Yes.", "That one.",
    "Airport Road.", "Unmapped Place.", "Manipal Hospital, Old Airport Road and tell me the distance.",
    "I mean Manipal.",
    "Navigate to Manipal Hospital, Old Airport Road.",
])
async def test_nonliteral_answer_does_not_acquire_continuation(build, answer):
    target = "Manipal Hospital, Old Airport Road"
    # A complete new navigation command uses its existing independent contract.
    next_plan = plan(target) if answer.startswith('Navigate') else lookup(target)
    coordinator, model, bus, _, _ = build([plan('Airport Road'), next_plan])
    try:
        coordinator.on_user_turn('Navigate to Airport Road.')
        await coordinator.drain()
        coordinator.on_user_turn(answer)
        await coordinator.drain()
        assert bus.of_type(E.PLAN_READY)[-1].data['continuation'] is None
        assert len(model.requests) == 2
        assert not bus.of_type(E.PLAN_REJECTED)
    finally:
        await coordinator.aclose()


@pytest.mark.parametrize('boundary', ['stale', 'invalid', 'wrong_query', 'stop', 'not_found',
                                     'done', 'blocked', 'unknown'])
async def test_continuation_requires_matching_current_destination_error_without_prior_effect(build, boundary):
    coordinator, _, _, _, _ = build([plan('Airport Road', search=True)])
    try:
        coordinator.on_user_turn('Navigate to Airport Road.')
        await coordinator.drain()
        previous, execution = coordinator._request_context
        execution = replace(execution, calls=deepcopy(execution.calls), outcomes=deepcopy(execution.outcomes))
        if boundary == 'stale':
            execution.stale = True
        elif boundary == 'invalid':
            execution.validation_error = 'invalid dependency'
        elif boundary in {'wrong_query', 'stop', 'not_found'}:
            result = execution.outcomes['lookup'].result
            key, value = {'wrong_query': ('query', 'Indiranagar'),
                          'stop': ('place_role', 'stop'),
                          'not_found': ('code', 'place_not_found')}[boundary]
            result[key] = value
        else:
            execution.outcomes['activate'] = CallOutcome('activate', 'start_navigation',
                                                        {'route_id': 'R0'}, boundary)
        assert coordinator.executor.manifest.continue_request(
            'I mean Manipal Hospital, Old Airport Road.', previous, execution) is None
    finally:
        await coordinator.aclose()


@pytest.mark.parametrize("previous,query", [
    ('Find Airport Road.', 'Airport Road'),
    ('How far is Airport Road?', 'Airport Road'),
    ('Do not navigate to Airport Road.', 'Airport Road'),
    ('Navigate to Airport Road and tell me the distance.', 'Airport Road and tell me the distance'),
    ('Navigate to Airport Road via Cubbon Park.', 'Airport Road via Cubbon Park'),
    ('Navigate to Airport Road without tolls.', 'Airport Road without tolls'),
    ('Take me to Unmapped Place.', 'Unmapped Place'),
])
async def test_destination_answer_cannot_replace_non_action_or_qualified_request(build, previous, query):
    target = 'Manipal Hospital, Old Airport Road'
    coordinator, model, bus, _, _ = build([lookup(query), lookup(target)])
    try:
        coordinator.on_user_turn(previous)
        await coordinator.drain()
        coordinator.on_user_turn(f'I mean {target}.')
        await coordinator.drain()
        assert bus.of_type(E.PLAN_READY)[-1].data['continuation'] is None
        assert len(model.requests) == 2
        assert not [e for e in bus.of_type(E.TOOL_DONE) if e.data['tool'] == 'start_navigation']
    finally:
        await coordinator.aclose()


@pytest.mark.parametrize('omitted', [lookup('Cubbon Park'), {'calls': [], 'reply': 'Okay.'},
                                    {**plan('Cubbon Park'), 'complete': False}])
async def test_grounded_answer_requires_complete_action_even_after_empty_plan(build, omitted):
    coordinator, model, bus, _, spoken = build([plan('Road'), omitted, plan('Cubbon Park')])
    try:
        coordinator.on_user_turn('Take me to Road.')
        await coordinator.drain()
        coordinator.on_user_turn('I mean Cubbon Park.')
        await coordinator.drain()
        assert len(model.requests) == 3
        assert len(bus.of_type(E.PLAN_REJECTED)) == 1
        assert 'Cubbon Park' in spoken[-1] and 'Estimated arrival' in spoken[-1]
        for _, prompt in model.requests[1:]:
            assert 'LATEST USER UTTERANCE\n"I mean Cubbon Park."' in prompt
            assert 'Take me to Road.' in prompt
            assert '"request": "Navigate to Cubbon Park."' in prompt
    finally:
        await coordinator.aclose()
