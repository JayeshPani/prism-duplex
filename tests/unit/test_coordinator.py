"""Coordinator unit tests: scripted plans, no audio, no network."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent.coordinator import events as E
from agent.coordinator.coordinator import Coordinator, CoordinatorConfig
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger, action_key
from agent.coordinator.refs import resolve_args
from agent.coordinator.resolver import IntentResolver
from agent.tools.manifest import Manifest, ToolSpec


def make_manifest(delays: dict[str, float] | None = None) -> Manifest:
    delays = delays or {}
    m = Manifest("test")

    def tool(name: str, state: bool, result):
        async def fn(**kw):
            await asyncio.sleep(delays.get(name, 0))
            return result(**kw)
        m.add(ToolSpec(name, name, {"x": {"type": "string"}, "n": {"type": "integer"}}, [], state, fn))

    tool("search", False, lambda **kw: {"items": [{"item_id": "IT1", "q": kw.get("x")}]})
    tool("buy", True, lambda **kw: {"ok": True, **kw})
    return m


class ScriptedLLM:
    """Returns a canned plan chosen by a substring of the utterance."""

    def __init__(self, plans: dict[str, dict], delay: float = 0.0) -> None:
        self.plans = plans
        self.delay = delay
        self.seen: list[str] = []

    async def complete_json(self, system: str, user: str) -> dict:
        utter = user.split("LATEST USER UTTERANCE\n")[1].split("\n")[0].strip("\"")
        self.seen.append(utter)
        await asyncio.sleep(self.delay)
        hits = [(utter.rfind(k), k) for k in self.plans if k in utter]
        if hits:
            return self.plans[max(hits)[1]]   # latest mention wins, like a real repair
        return {"complete": True, "calls": [], "reply": "ok"}


def build(plans, delays=None, llm_delay=0.0, cfg=None):
    m = make_manifest(delays)
    bus, ledger = EventBus(), Ledger()
    logged: list[tuple[str, dict]] = []
    ex = Executor(m, ledger, bus, lambda f, a, t0, t1: logged.append((f, a)))
    llm = ScriptedLLM(plans, llm_delay)
    said: list[str] = []

    async def speak(t):
        said.append(t)

    coord = Coordinator(IntentResolver(llm, m), ex, ledger, bus, speak, None,
                        cfg or CoordinatorConfig(hold_read_ms=100, hold_state_ms=150,
                                                 hold_incomplete_ms=300, ack_after_ms=10_000))
    return coord, bus, logged, said, llm


def test_refs_resolution():
    results = {"c1": {"flights": [{"flight_id": "FL9"}]}}
    assert resolve_args({"a": "$c1.flights[0].flight_id", "b": 3}, results) == {"a": "FL9", "b": 3}
    # forgiving path: id is nested deeper than the planner guessed
    assert resolve_args({"a": "$c1.flight_id"}, results) == {"a": "FL9"}


def test_ledger_key_is_canonical():
    assert action_key("book", {"name": "Ana Ruiz"}) == action_key("book", {"name": " ana_ruiz "})
    assert action_key("add", {"qty": 2.0}) == action_key("add", {"qty": 2})


@pytest.mark.asyncio
async def test_chain_and_duplicate_block():
    m = make_manifest()
    bus, ledger, logged = EventBus(), Ledger(), []
    ex = Executor(m, ledger, bus, lambda f, a, t0, t1: logged.append((f, a)))
    calls = [PlannedCall("c1", "search", {"x": "lamp"}),
             PlannedCall("c2", "buy", {"x": "$c1.items[0].item_id", "n": "2"})]
    r = await ex.run(calls)
    assert [o.status for o in r.outcomes.values()] == ["done", "done"]
    assert logged[1] == ("buy", {"x": "IT1", "n": 2})
    # same state-changing action again -> blocked, not executed, not logged
    r2 = await ex.run([PlannedCall("c1", "buy", {"x": "IT1", "n": 2})])
    assert r2.outcomes["c1"].status == "blocked"
    assert len(logged) == 2
    assert bus.of_type(E.TOOL_BLOCKED)


@pytest.mark.asyncio
async def test_resume_during_hold_discards_stale_plan():
    plans = {
        "to Paris": {"complete": True, "calls": [{"id": "c1", "tool": "search", "args": {"x": "Paris"}}], "reply": "Paris"},
        "Berlin": {"complete": True, "calls": [{"id": "c1", "tool": "search", "args": {"x": "Berlin"}}], "reply": "Berlin"},
    }
    coord, bus, logged, said, llm = build(plans)
    coord.on_user_turn("flights to Paris")
    await asyncio.sleep(0.03)             # inside the hold window
    coord.on_user_speaking()              # user keeps talking...
    coord.on_user_turn("actually no, Berlin")
    await coord.drain()
    assert logged == [("search", {"x": "Berlin"})]
    assert llm.seen[-1] == "flights to Paris actually no, Berlin"   # merged, re-resolved
    assert bus.of_type(E.GATE_CANCELLED)


@pytest.mark.asyncio
async def test_incomplete_utterance_waits_longer():
    plans = {"to the": {"complete": False, "calls": [{"id": "c1", "tool": "search", "args": {"x": "?"}}], "reply": ""},
             "Rome": {"complete": True, "calls": [{"id": "c1", "tool": "search", "args": {"x": "Rome"}}], "reply": "Rome"}}
    coord, bus, logged, said, _ = build(plans)
    coord.on_user_turn("take me to the")
    await asyncio.sleep(0.2)              # longer than read hold, shorter than incomplete hold
    assert logged == []
    coord.on_user_speaking()
    coord.on_user_turn("Rome")
    await coord.drain()
    assert logged == [("search", {"x": "Rome"})]


@pytest.mark.asyncio
async def test_correction_after_commit_cancels_inflight_read():
    plans = {"airport": {"complete": True, "calls": [{"id": "c1", "tool": "search", "args": {"x": "airport"}}], "reply": "a"},
             "office": {"complete": True, "calls": [{"id": "c1", "tool": "search", "args": {"x": "office"}}], "reply": "o"}}
    coord, bus, logged, said, _ = build(plans, delays={"search": 0.5})
    coord.on_user_turn("route to the airport")
    await asyncio.sleep(0.25)             # committed, search in flight
    coord.on_user_turn("no, the office first")
    await coord.drain()
    assert logged == [("search", {"x": "office"})]
    assert bus.of_type(E.TOOL_CANCELLED)


@pytest.mark.asyncio
async def test_unknown_tool_never_executes():
    plans = {"hack": {"complete": True, "calls": [{"id": "c1", "tool": "rm_rf", "args": {}}], "reply": "no"}}
    coord, bus, logged, said, _ = build(plans)
    coord.on_user_turn("hack it")
    await coord.drain()
    assert logged == [] and said == ["no"]


@pytest.mark.asyncio
async def test_ack_fills_silence_when_resolver_is_slow():
    plans = {"x": {"complete": True, "calls": [], "reply": "done thinking"}}
    cfg = CoordinatorConfig(hold_read_ms=0, hold_state_ms=0, ack_after_ms=50)
    coord, bus, logged, said, _ = build(plans, llm_delay=0.2, cfg=cfg)
    coord.on_user_turn("x")
    await coord.drain()
    await asyncio.sleep(0.01)
    assert said[0] in cfg.ack_phrases and said[-1] == "done thinking"


@pytest.mark.asyncio
async def test_noise_blip_pauses_then_commits_same_plan():
    plans = {"order": {"complete": True, "calls": [{"id": "c1", "tool": "search", "args": {"x": "o1"}}], "reply": "ok"}}
    cfg = CoordinatorConfig(hold_read_ms=100, hold_state_ms=100, ack_after_ms=10_000, resume_grace_ms=150)
    coord, bus, logged, said, llm = build(plans, cfg=cfg)
    coord.on_user_turn("track my order")
    await asyncio.sleep(0.03)
    coord.on_user_speaking()              # breath / noise, no words follow
    await asyncio.sleep(0.2)
    assert logged == []                   # paused while "speaking"
    coord.on_user_listening()
    await coord.drain()
    assert logged == [("search", {"x": "o1"})]
    assert len(llm.seen) == 1             # not re-planned
