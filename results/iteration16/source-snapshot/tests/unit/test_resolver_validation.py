"""Independent development plans: malformed model output must fail as a whole."""

from __future__ import annotations

from copy import deepcopy

import pytest

from agent.coordinator import events as E
from agent.coordinator.coordinator import Coordinator, CoordinatorConfig
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor
from agent.coordinator.ledger import Ledger
from agent.coordinator.resolver import IntentResolver
from agent.tools.manifest import Manifest, ToolSpec


VALID_CALL = {"id": "first", "tool": "record_choice", "args": {"label": "window seat"}}


@pytest.fixture
def setup():
    effects = []
    manifest = Manifest("independent-validation-development")

    async def record_choice(label):
        effects.append(label)
        return {"status": "success", "label": label}

    async def clear_choice():
        effects.append("cleared")
        return {"status": "success"}

    manifest.add(ToolSpec("record_choice", "", {"label": {"type": "string"}}, ["label"], True, record_choice))
    manifest.add(ToolSpec("clear_choice", "", {}, [], True, clear_choice))
    return manifest, effects


INVALID_CALLS = [
    None, "record_choice", [],
    {"id": "other", "tool": "unknown_tool", "args": {}},
    {"id": "other", "tool": 123, "args": {}},
    {"id": "other", "tool": "record_choice", "args": []},
    {"id": "other", "tool": "record_choice", "args": None},
    {"id": "other", "tool": "record_choice", "args": "window seat"},
    {"id": "other", "tool": "record_choice", "args": {}},
    {"id": "other", "tool": "record_choice", "args": {"label": "unknown"}},
    {"id": "other", "tool": "record_choice", "args": {"label": None}},
    {"id": "other", "tool": "record_choice", "args": {"label": "?"}},
    {"id": "", "tool": "clear_choice", "args": {}},
    {"id": None, "tool": "clear_choice", "args": {}},
    {"id": 7, "tool": "clear_choice", "args": {}},
    {"id": "other", "tool": "clear_choice", "args": [], "arguments": {}},
    {"id": "other", "tool": "", "function": "clear_choice", "args": {}},
]


@pytest.mark.parametrize("bad_call", INVALID_CALLS)
def test_invalid_call_rejects_whole_plan(setup, bad_call):
    manifest, _ = setup
    with pytest.raises(ValueError):
        IntentResolver(None, manifest)._to_resolution({"complete": True, "calls": [VALID_CALL, bad_call]})


@pytest.mark.parametrize("field,value", [
    ("complete", "false"), ("complete", "true"), ("complete", 0), ("complete", 1), ("complete", None),
    ("calls", {}), ("calls", "record_choice"), ("calls", None),
    ("reply", []), ("reply", 42), ("repaired", {}),
    ("corrections", {}), ("corrections", ["not a correction"]),
])
def test_invalid_top_level_field_is_not_coerced(setup, field, value):
    manifest, _ = setup
    data = {"complete": True, "calls": [VALID_CALL], "reply": "Recording your choice.", "corrections": []}
    data[field] = value
    with pytest.raises(ValueError):
        IntentResolver(None, manifest)._to_resolution(data)


@pytest.mark.parametrize("data", [None, [], "not a plan"])
def test_top_level_nonobject_has_validation_error(setup, data):
    manifest, _ = setup
    with pytest.raises(ValueError):
        IntentResolver(None, manifest)._to_resolution(data)


@pytest.mark.parametrize("bad_call", [INVALID_CALLS[i] for i in (0, 3, 5, 8, 9)])
async def test_valid_write_beside_invalid_call_has_no_effects(setup, bad_call):
    manifest, effects = setup

    class Model:
        async def complete_json(self, *_):
            return {"complete": True, "calls": [deepcopy(VALID_CALL), bad_call], "reply": "Recording your choice."}

    bus, ledger, logged, spoken = EventBus(), Ledger(), [], []
    executor = Executor(manifest, ledger, bus, lambda tool, *args: logged.append(tool))

    async def speak(text):
        spoken.append(text)

    coordinator = Coordinator(IntentResolver(Model(), manifest), executor, ledger, bus, speak,
                              config=CoordinatorConfig(hold_read_ms=0, hold_state_ms=0, ack_after_ms=10_000))
    coordinator.on_user_turn("record my choice")
    try:
        await coordinator.drain()
        assert effects == logged == []
        assert not bus.of_type(E.TOOL_STARTED)
        assert spoken == ["Sorry, could you say that again?"]
        assert any(event.data["tool"] == "resolver" for event in bus.of_type(E.TOOL_ERROR))
    finally:
        await coordinator.aclose()


@pytest.mark.parametrize("data,complete,tool", [
    ({"complete": True, "calls": [VALID_CALL], "corrections": [], "reply": "Recording."}, True, "record_choice"),
    ({"complete": False, "calls": [VALID_CALL]}, False, "record_choice"),
    ({"calls": [VALID_CALL]}, True, "record_choice"),
    ({"calls": [{"name": "record_choice", "arguments": {"label": "aisle seat"}}]}, True, "record_choice"),
    ({"calls": [{"function": "clear_choice"}]}, True, "clear_choice"),
])
def test_valid_plans_and_existing_aliases_are_preserved(setup, data, complete, tool):
    manifest, _ = setup
    result = IntentResolver(None, manifest)._to_resolution(data)
    assert result.complete is complete
    assert len(result.calls) == 1
    assert result.calls[0].tool == tool
    assert isinstance(result.calls[0].id, str)


def test_valid_smalltalk_plan_needs_no_calls(setup):
    manifest, _ = setup
    result = IntentResolver(None, manifest)._to_resolution({"reply": "Hello."})
    assert result.calls == []
    assert result.reply == "Hello."
