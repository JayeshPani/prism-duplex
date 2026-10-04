"""Planner narration is not execution evidence; summaries retain the request."""

import asyncio

import pytest

from agent.coordinator import events as E
from agent.coordinator.coordinator import CoordinatorConfig
from tests.unit.test_coordinator import build


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["Your purchase is complete.", "Buying the first item."])
@pytest.mark.parametrize("speak_results", [False, True])
async def test_tool_plan_reply_waits_for_grounded_result_without_replacement(reply, speak_results):
    entered, release = asyncio.Event(), asyncio.Event()
    plan = {"complete": True, "calls": [{"id": "c1", "tool": "buy", "args": {"x": "first"}}], "reply": reply}
    cfg = CoordinatorConfig(hold_state_ms=0, ack_after_ms=10000, speak_results=speak_results)
    coord, bus, logged, said, _ = build({"first": plan}, cfg=cfg)

    async def buy(**args):
        entered.set()
        await release.wait()
        return {"ok": True, **args}

    class GroundedResponder:
        async def summarize(self, request, execution):
            assert execution.outcomes["c1"].status == "done"
            return "The first item was purchased."

    coord.executor.manifest.tools["buy"].fn = buy
    coord.responder = GroundedResponder()
    coord.on_user_turn("Buy the first item.")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert said == []
        assert coord.history == [{"role": "user", "text": "Buy the first item."}]
        assert bus.of_type(E.PLAN_READY)[0].data["reply"] == reply
    finally:
        release.set()
        await coord.drain()
        await coord.aclose()
    assert logged == [("buy", {"x": "first"})]
    assert coord.ledger.summary()[0]["status"] == "done"
    assert said == (["The first item was purchased."] if speak_results else [])
    assert all(event.data["kind"] == "result" for event in bus.of_type(E.AGENT_SAY))


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [None, "", "Buy the first item."])
async def test_summary_receives_request_instead_of_planner_narration(repaired):
    utterance = "Please buy the, uh, first item."
    plan = {"complete": True, "calls": [{"id": "c1", "tool": "buy", "args": {"x": "first"}}],
            "reply": "Buying the first item."}
    if repaired is not None:
        plan["repaired"] = repaired
    coord, bus, _, said, _ = build({"first": plan}, cfg=CoordinatorConfig(hold_state_ms=0, ack_after_ms=10000))
    requests = []

    class CapturingResponder:
        async def summarize(self, request, execution, *, repaired_request=None):
            requests.append((request, repaired_request))
            return "The first item was purchased."

    coord.responder = CapturingResponder()
    coord.on_user_turn(utterance)
    await coord.drain()
    await coord.aclose()
    assert requests == [(utterance, repaired or None)]
    assert bus.of_type(E.PLAN_READY)[0].data["repaired"] == (repaired or "")
    assert said == ["The first item was purchased."]


@pytest.mark.asyncio
async def test_slow_tool_plan_still_gets_acknowledgment_and_grounded_result():
    plan = {"complete": True, "calls": [{"id": "c1", "tool": "buy", "args": {"x": "first"}}],
            "reply": "Your purchase is complete."}
    cfg = CoordinatorConfig(hold_state_ms=0, ack_after_ms=1)
    coord, bus, logged, said, _ = build({"first": plan}, llm_delay=.03, cfg=cfg)

    class GroundedResponder:
        async def summarize(self, request, execution):
            return "The first item was purchased."

    coord.responder = GroundedResponder()
    coord.on_user_turn("Buy the first item.")
    await coord.drain()
    await coord.aclose()
    assert said == [cfg.ack_phrases[0], "The first item was purchased."]
    assert [event.data["kind"] for event in bus.of_type(E.AGENT_SAY)] == ["ack", "result"]
    assert logged == [("buy", {"x": "first"})]
