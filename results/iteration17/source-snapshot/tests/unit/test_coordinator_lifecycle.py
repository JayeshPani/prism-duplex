"""Coordinator-owned tasks report failures and stop at session boundaries."""

import asyncio

import pytest

from agent.coordinator import events as E
from agent.coordinator.coordinator import CoordinatorConfig
from agent.coordinator.responder import Responder
from tests.unit.test_coordinator import build


def config():
    return CoordinatorConfig(hold_read_ms=0, hold_state_ms=0,
                             hold_incomplete_ms=0, ack_after_ms=10000)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_speech_failures_are_reported_and_next_turn_recovers(complete):
    plans = {"first": {"complete": complete, "calls": [], "reply": "first"},
             "second": {"complete": True, "calls": [], "reply": "second"}}
    coord, bus, _, _, _ = build(plans, cfg=config())
    spoken = []

    async def speak(text):
        if not spoken:
            spoken.append(text)
            raise RuntimeError("TTS disconnected")
        spoken.append(text)

    coord.speak_fn = speak
    coord.on_user_turn("first")
    await coord.drain()
    failures = bus.of_type(E.TOOL_ERROR)
    assert len(failures) == 1
    assert failures[0].data["tool"] == "speech"
    assert failures[0].data["intent_version"] == 1
    assert failures[0].data["error"] == "TTS disconnected"
    coord.on_user_turn("second")
    await coord.drain()
    assert len(spoken) == 2
    assert spoken[-1] == "second"
    await coord.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("real_responder", [False, True])
@pytest.mark.parametrize("superseded", [False, True])
async def test_result_generation_failure_is_observable_without_changing_tool_outcome(real_responder, superseded):
    plans = {"first": {"complete": True, "calls": [{"id": "c1", "tool": "buy", "args": {"x": "first"}}], "reply": ""},
             "second": {"complete": True, "calls": [], "reply": "second"}}
    coord, bus, logged, said, _ = build(plans, cfg=config())
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(*args):
        entered.set()
        await release.wait()
        raise RuntimeError("result model unavailable")

    class BrokenLLM:
        complete = staticmethod(fail)

    class BrokenResponder:
        summarize = staticmethod(fail)

    coord.responder = Responder(BrokenLLM()) if real_responder else BrokenResponder()
    coord.on_user_turn("first")
    await asyncio.wait_for(entered.wait(), 1)
    if superseded:
        coord.on_user_turn("second")
    release.set()
    await coord.drain()
    failures = bus.of_type(E.TOOL_ERROR)
    assert len(failures) == 1
    assert failures[0].data["tool"] == "responder"
    assert failures[0].data["phase"] == "summary"
    assert failures[0].data["intent_version"] == 1
    assert failures[0].data["error"] == "result model unavailable"
    assert failures[0].data["id"] != "c1"
    assert logged == [("buy", {"x": "first"})]
    assert coord.ledger.summary()[0]["status"] == "done"
    assert len(bus.of_type(E.TOOL_DONE)) == 1
    assert said == (["second"] if superseded else ["I couldn't prepare a spoken summary of the result."])
    await coord.aclose()


@pytest.mark.asyncio
async def test_closed_session_ignores_vad_and_partial_callbacks():
    coord, bus, _, _, _ = build({}, cfg=config())
    await coord.aclose()
    events = list(bus.history)
    coord.on_user_speaking()
    coord.on_user_listening()
    coord.on_user_partial("late audio")
    assert coord._reopen_task is None
    assert bus.history == events


@pytest.mark.asyncio
async def test_correction_from_speech_event_prevents_stale_delivery():
    plans = {word: {"complete": True, "calls": [], "reply": word} for word in ("first", "second")}
    coord, bus, _, said, _ = build(plans, cfg=config())

    def correct(event):
        if event.type == E.AGENT_SAY and event.data["text"] == "first":
            coord.on_user_turn("second")

    bus.subscribe(correct)
    coord.on_user_turn("first")
    await coord.drain()
    assert said == ["second"]
    await coord.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["resolver", "speech", "responder"])
async def test_shutdown_is_bounded_when_owned_work_resists_cancellation(phase):
    entered, release = asyncio.Event(), asyncio.Event()
    plans = {"first": {"complete": True, "calls": [{"id": "c1", "tool": "search", "args": {"x": "first"}}], "reply": "first"}}
    if phase == "speech":
        plans["first"]["calls"] = []
    coord, bus, _, _, _ = build(plans, cfg=config())

    async def stubborn(*args):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return ""

    if phase == "resolver":
        coord.resolver.resolve = stubborn
    elif phase == "speech":
        coord.speak_fn = stubborn
    else:
        class SlowResponder:
            summarize = staticmethod(stubborn)
        coord.responder = SlowResponder()

    coord.on_user_turn("first")
    await asyncio.wait_for(entered.wait(), 1)
    close = asyncio.create_task(coord.aclose(timeout=.01))
    try:
        _, pending = await asyncio.wait({close}, timeout=.5)
        assert not pending, "shutdown ignored its deadline"
        assert any("cancellation" in e.data.get("error", "") for e in bus.of_type(E.TOOL_ERROR))
    finally:
        release.set()
        await asyncio.wait_for(close, 1)
        await coord.drain()


@pytest.mark.asyncio
async def test_replaced_resolver_remains_owned_until_it_finishes():
    coord, bus, logged, said, _ = build({}, cfg=config())
    entered, release = asyncio.Event(), asyncio.Event()
    resolve = coord.resolver.resolve

    async def delayed(utterance, *args):
        if utterance == "first":
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
        return await resolve(utterance, *args)

    coord.resolver.resolve = delayed
    coord.on_user_turn("first")
    await asyncio.wait_for(entered.wait(), 1)
    coord.on_user_turn("second")
    try:
        with pytest.raises(TimeoutError):
            await coord.drain(timeout=.02)
        await coord.aclose(timeout=.01)
        assert any("cancellation" in e.data.get("error", "") for e in bus.of_type(E.TOOL_ERROR))
    finally:
        release.set()
        await coord.drain()
        await coord.aclose()
    assert said == ["ok"]
    assert logged == []
