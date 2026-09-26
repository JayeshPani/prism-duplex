"""Scripted correction boundaries; no model, microphone or transport claims."""
import asyncio

import pytest

from agent.coordinator.coordinator import CoordinatorConfig
from tests.unit.test_coordinator import build

PLANS = {
    "first": {"complete": True, "calls": [{"id": "c1", "tool": "search", "args": {"x": "first"}}], "reply": ""},
    "second": {"complete": True, "calls": [{"id": "c1", "tool": "search", "args": {"x": "second"}}], "reply": ""},
}


@pytest.mark.asyncio
async def test_new_transcript_invalidates_old_read_before_next_commit():
    cfg = CoordinatorConfig(hold_read_ms=10, ack_after_ms=10000)
    coord, bus, logged, said, _ = build(PLANS, delays={"search": .12}, cfg=cfg)
    coord.on_user_turn("first")
    await asyncio.sleep(.03)
    coord.cfg.hold_read_ms = 200
    coord.on_user_turn("second")
    await coord.drain()
    old = [e for e in bus.history if e.type == "tool_done" and e.data.get("args", {}).get("x") == "first"]
    assert old == []
    assert logged[-1] == ("search", {"x": "second"})


@pytest.mark.asyncio
async def test_correction_during_result_generation_suppresses_old_speech():
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowResponder:
        async def summarize(self, request, ex):
            if any(o.args.get("x") == "first" for o in ex.outcomes.values()):
                entered.set()
                await release.wait()
                return "obsolete first result"
            return "current second result"

    coord, _, _, said, _ = build(PLANS, cfg=CoordinatorConfig(hold_read_ms=0, ack_after_ms=10000))
    coord.responder = SlowResponder()
    coord.on_user_turn("first")
    await asyncio.wait_for(entered.wait(), 2)
    coord.on_user_turn("second")
    release.set()
    await coord.drain()
    assert "obsolete first result" not in said
    assert "current second result" in said


@pytest.mark.asyncio
async def test_incomplete_plan_never_guesses_an_action_after_timer():
    plans = {"first": {**PLANS["first"], "complete": False, "reply": "Searching first"}}
    coord, _, logged, said, _ = build(plans, cfg=CoordinatorConfig(hold_incomplete_ms=5, ack_after_ms=10000))
    coord.on_user_turn("first and")
    await coord.drain()
    assert logged == []
    assert said and "finish" in said[-1].lower()


@pytest.mark.asyncio
async def test_correction_between_gate_and_commit_preserves_pending_request():
    coord, _, _, _, llm = build(PLANS, cfg=CoordinatorConfig(hold_read_ms=0, ack_after_ms=10000))
    coord.on_user_turn("first")
    await asyncio.sleep(0)  # gate finished, scheduled commit has not run
    coord.on_user_turn("second")
    await coord.drain()
    assert llm.seen[-1] == "first second"


@pytest.mark.asyncio
async def test_vad_blip_does_not_irreversibly_cancel_active_speech():
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []
    coord, _, _, _, _ = build({}, cfg=CoordinatorConfig(hold_read_ms=0, ack_after_ms=10000, resume_grace_ms=1))

    async def speak(text):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(text)
            raise

    coord.speak_fn = speak
    coord.on_user_turn("hello")
    await asyncio.wait_for(entered.wait(), 2)
    coord.on_user_speaking()
    coord.on_user_listening()
    coord.on_user_turn("")
    release.set()
    await coord.drain()
    assert not cancelled
    await coord.aclose()


@pytest.mark.asyncio
async def test_disconnect_cleans_up_owned_tasks_and_ignores_further_turns():
    coord, _, logged, _, _ = build(PLANS, llm_delay=.5)
    coord.on_user_turn("first")
    await asyncio.sleep(0)
    await coord.aclose(timeout=.05)
    coord.on_user_turn("second")
    await coord.drain(timeout=.1)
    assert logged == []
