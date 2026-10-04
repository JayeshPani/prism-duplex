"""Scorer/runner controls only; fake model output is never evaluation evidence."""
import io
import json

import pytest

from agent.coordinator.executor import CallOutcome, PlannedCall
from agent.coordinator.llm_client import LLMClient, LLMConfig
from agent.tools.car_tools import build_car_manifest
from scripts.planner_fidelity_experiment import CASES, assess, run_case


def call(tool, args, id=None):
    return PlannedCall(id or tool, tool, args)


def outcomes(calls):
    return {c.id: CallOutcome(c.id, c.tool, c.args, "done", result={}) for c in calls}


def test_named_stop_rejects_category_substitution_even_with_correct_effect():
    calls = [call("find_nearby", {"category": "coffee"}),
             call("add_waypoint", {"place_id": "$find_nearby.places[0].place_id"})]
    result = outcomes(calls)
    result["add_waypoint"].args = {"place_id": "K_TWC_HEB"}
    attempts = [{"tool": "add_waypoint", "state_changing": True,
                 "result": {"stop_ids": ["K_TWC_HEB"], "destination_id": "P_WHITEFIELD"}}]
    actual = assess(CASES["named_stop"], calls, result, attempts)
    assert actual["mock_execution_passed"]
    assert not actual["plan_fidelity_passed"]
    assert not actual["passed"]


@pytest.mark.parametrize("name,args", [
    ("category_no_cap", {"category": "coffee", "max_detour_min": 5}),
    ("category_explicit_cap", {"category": "coffee"}),
    ("product_no_budget", {"query": "cedar travel pouch", "max_price": 50}),
    ("product_explicit_budget", {"query": "cedar travel pouch"}),
])
def test_invented_or_missing_requested_filter_fails(name, args):
    calls = [call(CASES[name]["tools"][0], args)]
    result = assess(CASES[name], calls, outcomes(calls), [])
    assert not result["checks"]["arguments_grounded"]
    assert not result["passed"]


@pytest.mark.parametrize("reverse", [False, True])
def test_independent_actions_accept_different_ids_and_either_order(reverse):
    calls = [call("search_products", {"query": "cedar travel pouch"}, "alpha"),
             call("track_order", {"order_id": "LM702"}, "omega")]
    if reverse:
        calls.reverse()
    assert assess(CASES["two_independent_actions"], calls, outcomes(calls), [])["passed"]
    assert not assess(CASES["two_independent_actions"], calls[:1], outcomes(calls[:1]), [])["passed"]


def test_cancelled_search_must_not_execute_even_if_tracking_also_succeeds():
    calls = [call("search_products", {"query": "cedar travel pouch"}), call("track_order", {"order_id": "LM702"})]
    assert not assess(CASES["cancel_search_action"], calls, outcomes(calls), [])["passed"]
    assert assess(CASES["cancel_search_action"], calls[1:], outcomes(calls[1:]), [])["passed"]


def test_cart_requires_dependency_not_an_invented_product_id():
    calls = [call("search_products", {"query": "cedar travel pouch"}), call("add_to_cart", {"product_id": "catalog-result", "quantity": 2})]
    result = outcomes(calls)
    result["search_products"].result = {"products": [{"product_id": "catalog-result"}]}
    attempts = [{"tool": "add_to_cart", "state_changing": True, "result": {"product_id": "catalog-result", "quantity": 2}}]
    actual = assess(CASES["search_and_add"], calls, result, attempts)
    assert actual["mock_execution_passed"]
    assert not actual["checks"]["dependencies_correct"]


async def test_named_target_is_not_first_nearby_result_for_declared_setup(monkeypatch):
    monkeypatch.setattr("agent.tools.car_tools.ROUTE_COMPUTE_S", 0)
    manifest = build_car_manifest()
    route = await manifest.get("compute_route").fn(destination="Whitefield")
    await manifest.get("start_navigation").fn(route_id=route["route_id"])
    nearby = await manifest.get("find_nearby").fn(category="coffee")
    assert nearby["places"][0]["place_id"] != CASES["named_stop"]["target"]
    assert CASES["named_stop"]["target"] in {p["place_id"] for p in nearby["places"]}


@pytest.mark.parametrize("name", CASES)
async def test_runner_uses_real_mocks_and_preserves_declared_setup(monkeypatch, name):
    monkeypatch.setattr("agent.tools.car_tools.ROUTE_COMPUTE_S", 0)
    case = CASES[name]
    planned = []
    for tool in case["tools"]:
        if tool == "find_nearby":
            args = case["args"]
        elif tool == "search_products":
            args = {"query": "cedar travel pouch"}
            if case.get("budget") is not None:
                args["max_price"] = case["budget"]
        elif tool == "add_to_cart":
            args = {"product_id": "$search_products.products[0].product_id", "quantity": 2}
        elif tool == "add_waypoint":
            args = {"place_id": "Third Wave Coffee, Hebbal"}
        else:
            args = {"order_id": "LM702"}
        planned.append({"id": tool, "tool": tool, "args": args})

    async def fake_complete(*args, **kwargs):
        return json.dumps({"complete": True, "calls": planned, "reply": "Working on that."})

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    base = LLMClient(LLMConfig("http://127.0.0.1:8081/v1", "test-only"))
    trace = io.StringIO()
    try:
        result = await run_case(name, case, base, trace)
        assert result["passed"], result
        records = [json.loads(line) for line in trace.getvalue().splitlines()]
        assert any(r["kind"] == "llm_response" for r in records)
        assert any(r["kind"] == "backend_dispatch" and r["phase"] == "evaluation" for r in records)
        if case["domain"] == "car":
            setup = next(r for r in records if r["kind"] == "explicit_setup_context")
            assert setup["snapshot"]["destination_id"] == "P_WHITEFIELD"
            request = next(r for r in records if r["kind"] == "llm_request")
            assert "Whitefield" in request["user"]
    finally:
        await base.client.close()


@pytest.mark.parametrize("query,expected", [
    ("Third Wave Coffee in Hebbal", True), ("Third Wave Coffee, Hebbal", True), ("coffee", False),
])
def test_named_lookup_preserves_name_even_if_generic_query_would_find_target(query, expected):
    calls = [call("search_destination", {"query": query}, "lookup"),
             call("add_waypoint", {"place_id": "$lookup.places[0].place_id"}, "stop")]
    result = outcomes(calls)
    result["lookup"].result = {"places": [{"place_id": "K_TWC_HEB"}]}
    result["stop"].args = {"place_id": "K_TWC_HEB"}
    attempts = [{"tool": "add_waypoint", "state_changing": True,
                 "result": {"stop_ids": ["K_TWC_HEB"], "destination_id": "P_WHITEFIELD"}}]
    assert assess(CASES["named_stop"], calls, result, attempts)["passed"] is expected


def test_null_optional_filter_is_not_an_invented_filter():
    calls = [call("search_products", {"query": "cedar travel pouch", "max_price": None})]
    assert assess(CASES["product_no_budget"], calls, outcomes(calls), [])["passed"]


def test_coerced_container_query_is_not_accepted_as_literal_user_query():
    calls = [call("search_products", {"query": ["cedar travel pouch"]})]
    result = outcomes(calls)
    result["search_products"].args = {"query": '["cedar travel pouch"]'}
    assert not assess(CASES["product_no_budget"], calls, result, [])["plan_fidelity_passed"]


async def test_incomplete_plan_never_dispatches_mock_actions(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return json.dumps({"complete": False, "calls": [
            {"id": "buy", "tool": "add_to_cart", "args": {"product_id": "guessed", "quantity": 2}}]})

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    base = LLMClient(LLMConfig("http://127.0.0.1:8081/v1", "test-only"))
    try:
        result = await run_case("search_and_add", CASES["search_and_add"], base, io.StringIO())
        assert result["attempts"] == []
        assert result["outcomes"] == {}
        assert not result["passed"]
    finally:
        await base.client.close()
