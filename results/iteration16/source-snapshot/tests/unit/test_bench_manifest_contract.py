"""Check the manifest against the pinned backend rather than benchmark items."""

import inspect

import pytest

from agent.coordinator import events as E
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.coordinator.resolver import IntentResolver
from agent.tools import bench_tools


@pytest.fixture
def manifest(monkeypatch):
    registry = bench_tools._registry("instant")
    # Contract tests exercise real mock functions without simulated network waits.
    monkeypatch.setattr(registry.injector, "inject", lambda _name: None)
    monkeypatch.setattr(bench_tools, "_registry", lambda _profile: registry)
    return bench_tools.build_bench_manifest(), registry


@pytest.mark.parametrize("name", [tool[0] for tool in bench_tools._TOOLS])
def test_declared_required_arguments_cover_pinned_backend_signature(manifest, name):
    schema, registry = manifest
    parameters = inspect.signature(registry.FUNCTIONS[name]).parameters.values()
    required = {p.name for p in parameters
                if p.default is inspect.Parameter.empty
                and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)}
    assert required <= set(schema.tools[name].required)


@pytest.mark.parametrize("missing", ["city", "bedrooms", "max_price"])
def test_resolver_rejects_under_specified_apartment_plan(manifest, missing):
    schema, _ = manifest
    args = {"city": "Salem", "bedrooms": 2, "max_price": 1800}
    del args[missing]
    with pytest.raises(ValueError, match="missing required detail"):
        IntentResolver(None, schema)._to_resolution({"calls": [
            {"id": "save", "tool": "update_search_filter", "args": {"filter_name": "pets_allowed", "value": True}},
            {"id": "search", "tool": "search_apartments", "args": args}]})


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["city", "bedrooms", "max_price"])
@pytest.mark.parametrize("search_first", [False, True])
async def test_missing_apartment_details_reject_whole_plan_before_write(manifest, missing, search_first):
    schema, _ = manifest
    args = {"city": "Salem", "bedrooms": 2, "max_price": 1800}
    del args[missing]
    calls = [PlannedCall("save", "update_search_filter", {"filter_name": "pets_allowed", "value": True}),
             PlannedCall("search", "search_apartments", args)]
    if search_first:
        calls.reverse()
    bus, ledger = EventBus(), Ledger()
    execution = await Executor(schema, ledger, bus).run(calls)
    assert execution.validation_error
    assert all(outcome.status == "error" for outcome in execution.outcomes.values())
    assert bus.of_type(E.TOOL_STARTED) == []
    assert ledger.summary() == []


@pytest.mark.asyncio
async def test_complete_apartment_arguments_and_other_backend_defaults_still_work(manifest):
    schema, _ = manifest
    resolution = IntentResolver(None, schema)._to_resolution({"calls": [
        {"id": "search", "tool": "search_apartments", "args": {"city": "Salem", "bedrooms": "2", "max_price": "1,800"}},
        {"id": "commute", "tool": "calculate_commute", "args": {"origin_address": "$search.results[0].id", "destination_address": "Museum"}},
        {"id": "products", "tool": "search_products", "args": {"query": "desk fan", "max_price": None}},
        {"id": "booking", "tool": "book_flight", "args": {"passenger_name": "Alex Lee"}}]})
    execution = await Executor(schema, Ledger(), EventBus()).run(resolution.calls)
    assert all(outcome.status == "done" for outcome in execution.outcomes.values())
    assert execution.outcomes["search"].args == {"city": "Salem", "bedrooms": 2, "max_price": 1800}
    assert execution.outcomes["search"].result["results"][0] == {"id": "APT1", "price": 1700, "beds": 2}
    assert execution.outcomes["commute"].result["mode"] == "driving"
    assert execution.outcomes["products"].result["products"][0]["price"] == 99.99
    assert execution.outcomes["booking"].result["passenger"] == "Alex Lee"
