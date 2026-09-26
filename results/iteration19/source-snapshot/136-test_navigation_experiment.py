"""Harness/scorer checks only: these do not count as local-model experiments."""
from copy import deepcopy
import asyncio
import io
import json

import pytest

from agent.coordinator.llm_client import LLMClient, LLMConfig
from scripts.local_navigation_experiment import CASES, RecordedLLM, Session, evaluate


def observed(name):
    case = CASES[name]
    return {"effects": [{"tool": tool, "result": {"destination_id": destination, "stop_ids": stops}}
                        for tool, destination, stops in case["expected_effects"]],
            "final_destination": case["final"], "attempts": [], "runtime_errors": [], "error": None,
            "turns_submitted": len(case["turns"]),
            "replies": [{"turn": i, "kind": "result", "text": "Which place?"}
                        for i in range(1, len(case["turns"]) + 1)]}


@pytest.mark.parametrize("name", CASES)
def test_expected_effects_pass_and_wrong_effects_fail(name):
    run = observed(name)
    assert evaluate(name, run)["success"]
    run["effects"][0]["result"]["destination_id"] = "P_HOME"
    assert not evaluate(name, run)["success"]


def test_extra_duplicate_effect_is_not_hidden_by_correct_final_destination():
    run = observed("return_to_destination")
    run["effects"].append(deepcopy(run["effects"][-1]))
    assert not evaluate("return_to_destination", run)["success"]


def test_wrong_stop_fails_even_if_navigation_is_eventually_cancelled():
    run = observed("waypoint_and_cancel")
    run["effects"][1]["result"]["stop_ids"] = ["K_SBUX_AIR"]
    assert not evaluate("waypoint_and_cancel", run)["success"]


def test_clarification_must_precede_every_write_and_include_response_text():
    run = observed("ambiguous_then_explicit")
    run["attempts"] = [{"turn": 1, "state_changing": True, "status": "error"}]
    assert not evaluate("ambiguous_then_explicit", run)["success"]
    run["attempts"] = []
    run["replies"][0]["kind"] = "ack"
    assert not evaluate("ambiguous_then_explicit", run)["success"]


async def test_raw_invalid_and_repaired_outputs_are_both_recorded(monkeypatch):
    replies = iter(["invalid json", '{"complete": true, "calls": []}'])

    async def fake_complete(*args, **kwargs):
        return next(replies)

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    base = LLMClient(LLMConfig("http://127.0.0.1:8081/v1", "test-only"))
    records = []
    client = RecordedLLM(base, lambda kind, **data: records.append({"kind": kind, **data}))
    try:
        assert (await client.complete_json("system", "user"))["complete"] is True
        assert [r["raw"] for r in records if r["kind"] == "llm_response"] == ["invalid json", '{"complete": true, "calls": []}']
    finally:
        await base.client.close()


async def test_correction_hook_precedes_dispatch_with_real_coordinator(monkeypatch):
    monkeypatch.setattr("agent.tools.car_tools.ROUTE_COMPUTE_S", 0)

    async def fake_complete(self, system, user, json_stop=False):
        if not json_stop:
            return "Navigation started."
        destination = "office" if "Actually" in user else "airport"
        return json.dumps({"complete": True, "calls": [
            {"id": "route", "tool": "compute_route", "args": {"destination": destination}},
            {"id": "start", "tool": "start_navigation", "args": {"route_id": "$route.route_id"}}]})

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    base = LLMClient(LLMConfig("http://127.0.0.1:8081/v1", "test-only"))
    session = Session("correction_before_commit", base,
                      {"coordinator": {"hold_read_ms": 0, "hold_state_ms": 0, "ack_after_ms": 1000}}, io.StringIO())
    try:
        await session.run(2)
        run = {"attempts": session.attempts, "effects": session.effects, "replies": session.replies,
               "runtime_errors": session.runtime_errors, "error": None,
               "turns_submitted": session.turn, "final_destination": session.active}
        assert evaluate("correction_before_commit", run)["success"]
        assert all(a["turn"] == 2 for a in session.attempts)
    finally:
        await session.coordinator.aclose()
        await base.client.close()


async def test_stale_dispatch_and_llm_response_keep_original_turn(monkeypatch):
    """Deliberately bypass dispatch guards to verify the harness catches a bug."""
    monkeypatch.setattr("agent.tools.car_tools.ROUTE_COMPUTE_S", 0)

    async def fake_complete(*args, **kwargs):
        return "Old request completed after correction."

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    base = LLMClient(LLMConfig("http://127.0.0.1:8081/v1", "test-only"))
    trace = io.StringIO()
    session = Session("correction_before_commit", base, {"coordinator": {}}, trace)
    release, tasks = asyncio.Event(), []

    async def faulty_old_task():
        await release.wait()
        await session.coordinator.resolver.llm.complete("system", "old request")
        await session.coordinator.executor.manifest.get("compute_route").fn(destination="airport")

    def faulty_on_user_turn(text):
        if len(tasks) == 0:
            tasks.append(asyncio.create_task(faulty_old_task()))

    monkeypatch.setattr(session.coordinator, "on_user_turn", faulty_on_user_turn)
    try:
        session.submit("Navigate to the airport.")
        session.origins[1] -= 1  # Distinguish the two latency origins without sleeping.
        session.submit("Actually, go to the office instead.")
        release.set()
        await tasks[0]
        assert session.turn == 2
        assert session.attempts[0]["turn"] == 1
        assert session.attempts[0]["dispatch_ms"] >= 1000
        records = [json.loads(line) for line in trace.getvalue().splitlines()]
        assert all(record["turn"] == 1 for record in records
                   if record["kind"] in {"llm_request", "llm_response", "backend_dispatch", "backend_result"})
        run = observed("correction_before_commit")
        run["attempts"] = session.attempts
        result = evaluate("correction_before_commit", run)
        assert result["checks"]["effect_sequence"]
        assert not result["checks"]["no_superseded_dispatch"]
        assert not result["success"]
    finally:
        await session.coordinator.aclose()
        await base.client.close()
