"""Clarification context obeys turn ownership; no model or audio service needed."""
import asyncio
from copy import deepcopy

import pytest

from agent.coordinator import events as E
from agent.coordinator.responder import Responder
from tests.unit.test_navigation_clarification import build, lookup
from tests.unit.test_navigation_plan_contract import plan


ORIGINAL = "Navigate to Airport Road."
DESTINATION = "Manipal Hospital, Old Airport Road"
ANSWER = f"I mean {DESTINATION}."
NO_CALLS = {"complete": True, "calls": [], "reply": "All right."}


def starts(bus):
    return [e for e in bus.of_type(E.TOOL_DONE) if e.data["tool"] == "start_navigation"]


def continuation_for(bus, version):
    return next(e.data.get("continuation") for e in bus.of_type(E.PLAN_READY)
                if e.data["intent_version"] == version)


async def resistant_wait(release):
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass


@pytest.mark.parametrize("intervening", ["Never mind.", "What is the weather?"])
async def test_cancel_or_query_consumes_context_before_later_place_fragment(build, intervening):
    coord, model, bus, logged, _ = build([
        plan("Airport Road", search=True), NO_CALLS, lookup(DESTINATION)])
    coord.cfg.speak_results = False
    try:
        for utterance in (ORIGINAL, intervening, ANSWER):
            coord.on_user_turn(utterance)
            await coord.drain()
        assert continuation_for(bus, 2) is None
        assert continuation_for(bus, 3) is None
        assert len(model.requests) == 3
        assert all("GROUNDED REQUEST CONTINUATION" not in prompt for _, prompt in model.requests)
        assert not starts(bus)
        assert [tool for tool, _ in logged] == ["search_destination", "search_destination"]
        assert [h["text"] for h in coord.history if h["role"] == "user"] == [ORIGINAL, intervening, ANSWER]
    finally:
        await coord.aclose()


async def test_new_command_replaces_context_and_success_does_not_create_retry(build):
    coord, model, bus, _, _ = build([
        plan("Airport Road", search=True), plan("Cubbon Park"), lookup(DESTINATION)])
    coord.cfg.speak_results = False
    try:
        for utterance in (ORIGINAL, "Navigate to Cubbon Park.", ANSWER):
            coord.on_user_turn(utterance)
            await coord.drain()
        assert continuation_for(bus, 2) is None
        assert continuation_for(bus, 3) is None
        assert len(model.requests) == 3
        assert [e.data["result"]["destination_id"] for e in starts(bus)] == ["P_CUBBON"]
    finally:
        await coord.aclose()


async def test_split_uncommitted_clarification_keeps_context_once(build):
    coord, model, bus, _, _ = build([
        plan("Airport Road", search=True),
        {"complete": False, "calls": [], "reply": "Please finish the name."},
        lookup(DESTINATION), plan(DESTINATION, search=True), lookup("Office")])
    coord.cfg.speak_results = False
    try:
        coord.on_user_turn(ORIGINAL)
        await coord.drain()
        coord.on_user_turn("I mean Manipal")
        await coord.drain()
        assert not starts(bus)
        coord.on_user_turn("Hospital, Old Airport Road.")
        await coord.drain()
        assert continuation_for(bus, 3) == {"original_request": ORIGINAL,
                                           "request": f"Navigate to {DESTINATION}."}
        assert bus.of_type(E.USER_FINAL)[-1].data["text"] == "Hospital, Old Airport Road."
        assert bus.of_type(E.USER_FINAL)[-1].data["merged"] == ANSWER
        assert [e.data["attempt"] for e in bus.of_type(E.PLAN_REJECTED)] == [1]
        assert [e.data["result"]["destination_id"] for e in starts(bus)] == ["P_HOSP"]
        assert [h["text"] for h in coord.history if h["role"] == "user"] == [ORIGINAL, ANSWER]
        coord.on_user_turn("I mean Office.")
        await coord.drain()
        assert continuation_for(bus, 4) is None
        assert len(starts(bus)) == 1
        assert len(model.requests) == 5
    finally:
        await coord.aclose()


@pytest.mark.parametrize("close", [False, True])
async def test_late_execution_cannot_restore_context_after_new_turn_or_close(build, close):
    coord, model, bus, _, spoken = build([
        plan("Airport Road", search=True), NO_CALLS, lookup(DESTINATION)])
    coord.cfg.speak_results = False
    entered, release, newer_spoken = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = coord.executor.run

    async def delayed(calls, **kwargs):
        execution = await execute(calls, **kwargs)
        if kwargs["operation_id"] == "turn-1":
            assert not execution.stale
            entered.set()
            await resistant_wait(release)
        return execution

    coord.executor.run = delayed
    bus.subscribe(lambda e: newer_spoken.set() if e.type == E.AGENT_SAY
                  and e.data["text"] == "All right." else None)
    try:
        coord.on_user_turn(ORIGINAL)
        await asyncio.wait_for(entered.wait(), 1)
        if close:
            await asyncio.wait_for(coord.aclose(timeout=.01), .5)
        else:
            coord.on_user_turn("Never mind.")
            await asyncio.wait_for(newer_spoken.wait(), 1)
        release.set()
        await coord.drain()
        if close:
            prior_events = len(bus.history)
            coord.on_user_turn(ANSWER)
            assert len(bus.history) == prior_events
            assert coord._request_context is None and coord._pending_context is None
        else:
            coord.on_user_turn(ANSWER)
            await coord.drain()
            assert continuation_for(bus, 3) is None
            assert len(model.requests) == 3
        assert not starts(bus)
        assert not any("Which place" in text for text in spoken)
    finally:
        release.set()
        await coord.drain()
        await coord.aclose()


async def test_slow_old_summary_cannot_reinstall_consumed_context_or_speak(build):
    coord, model, bus, _, spoken = build([
        plan("Airport Road", search=True), NO_CALLS, lookup(DESTINATION)])
    entered, release, newer_spoken = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class DelayedSummary:
        async def summarize(self, request, execution, **kwargs):
            if request == ORIGINAL:
                entered.set()
                await release.wait()
                return "Old clarification must remain silent."
            return "A lookup result."

    coord.responder = DelayedSummary()
    bus.subscribe(lambda e: newer_spoken.set() if e.type == E.AGENT_SAY
                  and e.data["text"] == "All right." else None)
    try:
        coord.on_user_turn(ORIGINAL)
        await asyncio.wait_for(entered.wait(), 1)
        coord.on_user_turn("Never mind.")
        await asyncio.wait_for(newer_spoken.wait(), 1)
        release.set()
        await coord.drain()
        coord.on_user_turn(ANSWER)
        await coord.drain()
        assert continuation_for(bus, 3) is None
        assert not starts(bus)
        assert "Old clarification must remain silent." not in spoken
        assert len(model.requests) == 3
    finally:
        release.set()
        await coord.drain()
        await coord.aclose()


async def test_trace_mutation_cannot_change_continued_plan_or_summary_context(build):
    coord, model, bus, _, _ = build([
        plan("Airport Road", search=True), lookup(DESTINATION), plan(DESTINATION, search=True)])
    summary_requests = []
    real = Responder(model)

    class RecordingSummary:
        async def summarize(self, request, execution, **kwargs):
            summary_requests.append((request, deepcopy(kwargs)))
            return await real.summarize(request, execution, **kwargs)

    coord.responder = RecordingSummary()

    def mutate(event):
        if event.type in {E.PLAN_REJECTED, E.PLAN_READY} and event.data.get("continuation"):
            event.data["continuation"]["original_request"] = "Navigate to Office."
            event.data["continuation"]["request"] = "Navigate to Office."

    bus.subscribe(mutate)
    try:
        coord.on_user_turn(ORIGINAL)
        await coord.drain()
        coord.on_user_turn(ANSWER)
        await coord.drain()
        assert [e.data["result"]["destination_id"] for e in starts(bus)] == ["P_HOSP"]
        assert len(model.requests) == 3
        assert '"request": "Navigate to Manipal Hospital, Old Airport Road."' in model.requests[-1][1]
        assert summary_requests[-1] == (
            f"{ORIGINAL}\nClarification: {ANSWER}",
            {"repaired_request": "", "continued_request": f"Navigate to {DESTINATION}."})
    finally:
        await coord.aclose()


async def test_reentrant_correction_on_rejection_prevents_repair_and_dispatch(build):
    coord, model, bus, logged, _ = build([
        plan("Airport Road", search=True), lookup(DESTINATION), NO_CALLS, lookup("Office")])
    coord.cfg.speak_results = False
    bus.subscribe(lambda e: coord.on_user_turn("Never mind.") if e.type == E.PLAN_REJECTED else None)
    try:
        coord.on_user_turn(ORIGINAL)
        await coord.drain()
        coord.on_user_turn(ANSWER)
        await coord.drain()
        assert len(model.requests) == 3  # Initial, rejected continuation, newer turn; no repair.
        assert len(bus.of_type(E.PLAN_REJECTED)) == 1
        assert [tool for tool, _ in logged] == ["search_destination"]
        coord.on_user_turn("Office.")
        await coord.drain()
        assert continuation_for(bus, 4) is None
        assert not starts(bus)
    finally:
        await coord.aclose()
