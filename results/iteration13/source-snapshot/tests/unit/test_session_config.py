import pytest
from agent.config import make_coordinator_config, room_mode


def test_browser_assistant_does_not_inherit_benchmark_mode(monkeypatch):
    monkeypatch.delenv("MODE", raising=False)
    assert room_mode("web-demo") == "assistant"
    assert room_mode("car-demo") == "car"
    assert room_mode("eval-123") == "bench"


def test_explicit_mode_is_validated(monkeypatch):
    monkeypatch.setenv("MODE", "car")
    assert room_mode("eval-123") == "car"
    monkeypatch.setenv("MODE", "typo")
    with pytest.raises(ValueError):
        room_mode("web-demo")


def test_local_turn_detection_and_interruption_are_explicit():
    from agent.config import local_turn_handling
    options = local_turn_handling()
    assert options["turn_detection"] == "vad"
    assert options["interruption"]["mode"] == "vad"
    assert options["interruption"]["resume_false_interruption"] is True


def test_benchmark_defaults_to_configured_result_speech(monkeypatch):
    monkeypatch.delenv("PRISM_BENCH_SPEAK_RESULTS", raising=False)
    from agent.config import load_config
    cfg = load_config("local-mac")
    assert make_coordinator_config(cfg, mode="bench").speak_results is True


@pytest.mark.parametrize("value, expected", [("0", False), ("1", True)])
def test_explicit_result_speech_ablation_is_benchmark_only(monkeypatch, value, expected):
    monkeypatch.setenv("PRISM_BENCH_SPEAK_RESULTS", value)
    cfg = {"coordinator": {"speak_results": True}}
    assert make_coordinator_config(cfg, mode="bench").speak_results is expected
    assert make_coordinator_config(cfg, mode="assistant").speak_results is True
    assert make_coordinator_config(cfg, mode="car").speak_results is True
    assert cfg["coordinator"]["speak_results"] is True


@pytest.mark.parametrize("value", ["", "true", "typo"])
def test_invalid_result_speech_override_cannot_silently_mute(monkeypatch, value):
    monkeypatch.setenv("PRISM_BENCH_SPEAK_RESULTS", value)
    with pytest.raises(ValueError, match="must be 0 or 1"):
        make_coordinator_config({"coordinator": {}}, mode="bench")
