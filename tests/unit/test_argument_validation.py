"""Reject known malformed literals before effects; references are not transactions."""

from copy import deepcopy

import pytest

from agent.coordinator import events as E
from agent.coordinator.coordinator import Coordinator, CoordinatorConfig
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.coordinator.resolver import IntentResolver
from agent.tools.manifest import Manifest, ToolSpec


INVALID_ARGUMENTS = [
    pytest.param({"quantity": 1.5}, id="fractional-integer"),
    pytest.param({"quantity": "1.5"}, id="fractional-integer-string"),
    pytest.param({"quantity": True}, id="boolean-as-integer"),
    pytest.param({"quantity": "many"}, id="nonnumeric-integer-string"),
    pytest.param({"amount": True}, id="boolean-as-number"),
    pytest.param({"amount": "NaN"}, id="nonfinite-number-string"),
    pytest.param({"quantity": float("inf")}, id="infinite-integer"),
    pytest.param({"amount": float("nan")}, id="nonfinite-number"),
    pytest.param({"quantity": "$lookup..quantity"}, id="malformed-numeric-reference"),
    pytest.param({"enabled": "sometimes"}, id="unknown-boolean-string"),
    pytest.param({"enabled": 2}, id="arbitrary-number-as-boolean"),
    pytest.param({"enabled": []}, id="array-as-boolean"),
    pytest.param({"label": ["desk"]}, id="array-as-string"),
    pytest.param({"label": {"name": "desk"}}, id="object-as-string"),
    pytest.param({"invented_argument": "desk"}, id="unknown-key"),
]


@pytest.fixture
def setup():
    manifest = Manifest("independent-argument-validation")
    effects = []

    async def write_marker(label):
        effects.append(("write_marker", {"label": label}))
        return {"status": "success", "label": label}

    async def write_options(**args):
        effects.append(("write_options", deepcopy(args)))
        return {"status": "success", "saved": args}

    manifest.add(ToolSpec("write_marker", "", {"label": {"type": "string"}},
                          ["label"], True, write_marker))
    manifest.add(ToolSpec("write_options", "", {
        "quantity": {"type": "integer"}, "amount": {"type": "number"},
        "enabled": {"type": "boolean"}, "label": {"type": "string"},
        "value": {"description": "untyped saved filter value"}}, [], True, write_options))
    bus, ledger = EventBus(), Ledger()
    executor = Executor(manifest, ledger, bus, lambda *args: None)
    return manifest, executor, bus, ledger, effects


@pytest.mark.parametrize("args", INVALID_ARGUMENTS)
def test_resolver_rejects_known_invalid_literal_plan_before_returning_calls(setup, args):
    manifest, _, _, _, _ = setup
    plan = {"calls": [{"id": "good", "tool": "write_marker", "args": {"label": "approved"}},
                       {"id": "bad", "tool": "write_options", "args": args}]}
    with pytest.raises(ValueError):
        IntentResolver(None, manifest)._to_resolution(plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("args", INVALID_ARGUMENTS)
async def test_executor_rejects_literal_malformed_sibling_before_any_effect(setup, args, bad_first):
    _, executor, bus, ledger, effects = setup
    calls = [PlannedCall("good", "write_marker", {"label": "approved"}),
             PlannedCall("bad", "write_options", args)]
    if bad_first:
        calls.reverse()
    execution = await executor.run(calls)
    assert effects == []
    assert ledger.summary() == []
    assert bus.of_type(E.TOOL_STARTED) == []
    assert execution.validation_error
    assert all(outcome.status == "error" for outcome in execution.outcomes.values())


@pytest.mark.asyncio
async def test_coordinator_does_not_commit_valid_write_beside_invalid_typed_sibling(setup):
    manifest, executor, bus, ledger, effects = setup

    class Planner:
        async def complete_json(self, *_):
            return {"complete": True, "calls": [
                {"id": "good", "tool": "write_marker", "args": {"label": "approved"}},
                {"id": "bad", "tool": "write_options", "args": {"enabled": "sometimes"}}],
                "reply": "Saving both preferences."}

    said = []

    async def speak(text):
        said.append(text)

    coordinator = Coordinator(IntentResolver(Planner(), manifest), executor, ledger, bus, speak,
                              config=CoordinatorConfig(hold_read_ms=0, hold_state_ms=0, ack_after_ms=10000))
    try:
        coordinator.on_user_turn("Save my preferences.")
        await coordinator.drain()
    finally:
        await coordinator.aclose()
    assert effects == []
    assert ledger.summary() == []
    assert said == ["Sorry, could you say that again?"]
    assert any(e.data.get("tool") == "resolver" for e in bus.of_type(E.TOOL_ERROR))


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,expected", [("yes", True), ("true", True), ("1", True),
                                             ("no", False), ("false", False), ("0", False),
                                             (True, True), (False, False), (1, True), (0, False)])
async def test_documented_compatible_scalar_coercions_and_untyped_values_remain_valid(setup, enabled, expected):
    manifest, executor, _, _, effects = setup
    args = {"quantity": "1,200", "amount": "45.5", "enabled": enabled,
            "label": "desk", "value": {"nested": [True, "large"]}}
    resolution = IntentResolver(None, manifest)._to_resolution({"calls": [
        {"id": "options", "tool": "write_options", "args": args}]})
    execution = await executor.run(resolution.calls)
    assert execution.outcomes["options"].status == "done"
    assert effects == [("write_options", {**args, "quantity": 1200, "amount": 45.5, "enabled": expected})]


@pytest.mark.asyncio
async def test_optional_none_and_omitted_default_keep_backend_semantics(setup):
    manifest, executor, _, _, effects = setup

    async def with_defaults(quantity=1, max_price=None):
        effects.append((quantity, max_price))
        return {"status": "success"}

    manifest.add(ToolSpec("with_defaults", "", {
        "quantity": {"type": "integer", "description": "defaults to 1"},
        "max_price": {"type": "number"}}, [], True, with_defaults))
    execution = await executor.run([PlannedCall("default", "with_defaults", {}),
                                    PlannedCall("null", "with_defaults", {"max_price": None})])
    assert effects == [(1, None), (1, None)]
    assert all(outcome.status == "done" for outcome in execution.outcomes.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved_quantity,valid", [("3", True), (1.5, False), ("$another.quantity", False)])
async def test_dynamic_reference_is_validated_after_lookup_without_transaction_claim(setup, resolved_quantity, valid):
    manifest, executor, bus, ledger, effects = setup

    async def lookup():
        return {"quantity": resolved_quantity}

    manifest.add(ToolSpec("lookup", "", {}, [], False, lookup))
    plan = {"calls": [
        {"id": "independent", "tool": "write_marker", "args": {"label": "approved"}},
        {"id": "lookup", "tool": "lookup", "args": {}},
        {"id": "dependent", "tool": "write_options", "args": {"quantity": "$lookup.quantity"}}]}
    resolution = IntentResolver(None, manifest)._to_resolution(plan)
    execution = await executor.run(resolution.calls)
    assert execution.outcomes["independent"].status == "done"
    assert execution.outcomes["lookup"].status == "done"
    assert ("write_marker", {"label": "approved"}) in effects
    if valid:
        assert execution.outcomes["dependent"].status == "done"
        assert ("write_options", {"quantity": 3}) in effects
    else:
        assert execution.outcomes["dependent"].status == "error"
        assert effects == [("write_marker", {"label": "approved"})]
        assert [event.data["tool"] for event in bus.of_type(E.TOOL_STARTED)] == ["write_marker", "lookup"]
        assert [entry["tool"] for entry in ledger.summary()] == ["write_marker"]


@pytest.mark.asyncio
async def test_nested_references_in_untyped_filter_value_remain_valid(setup):
    manifest, executor, _, _, effects = setup

    async def lookup():
        return {"tags": ["quiet", "shaded"]}

    manifest.add(ToolSpec("lookup", "", {}, [], False, lookup))
    resolution = IntentResolver(None, manifest)._to_resolution({"calls": [
        {"id": "lookup", "tool": "lookup", "args": {}},
        {"id": "save", "tool": "write_options", "args": {
            "value": {"selected": ["$lookup.tags[1]", {"count": 2}]}}}]})
    execution = await executor.run(resolution.calls)
    assert execution.outcomes["save"].status == "done"
    assert effects == [("write_options", {"value": {"selected": ["shaded", {"count": 2}]}})]


@pytest.mark.asyncio
async def test_required_null_sibling_is_rejected_before_any_effect(setup):
    _, executor, bus, _, effects = setup
    execution = await executor.run([PlannedCall("good", "write_marker", {"label": "approved"}),
                                    PlannedCall("bad", "write_marker", {"label": None})])
    assert execution.validation_error
    assert bus.of_type(E.TOOL_STARTED) == []
    assert effects == []


@pytest.mark.asyncio
async def test_integer_arguments_preserve_exact_values_without_float_roundtrip(setup):
    manifest, executor, _, _, effects = setup
    quantity = 9007199254740993
    resolution = IntentResolver(None, manifest)._to_resolution({"calls": [
        {"id": "integer", "tool": "write_options", "args": {"quantity": quantity}},
        {"id": "string", "tool": "write_options", "args": {"quantity": str(quantity)}}]})
    for call in resolution.calls:
        execution = await executor.run([call])
        assert execution.outcomes[call.id].status == "done"
    assert effects == [("write_options", {"quantity": quantity})] * 2
