"""Provenance and evaluation selection checks; no benchmark items or models."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import capture_run, fdb_eval


def local_config():
    return {"profile": "local-mac", "llm": {"base_url": "http://127.0.0.1:8081/v1", "model": "test/model"},
            "stt": {"kind": "parakeet-mlx", "model": "test/speech"}, "tts": {"kind": "kokoro"},
            "coordinator": {"speak_results": True}}


@pytest.mark.parametrize("url", ["https://example.com/v1", "http://127.0.0.1.example.com/v1",
                                  "http://127.0.0.1@remote.invalid/v1", "http://localhost/v1?api_key=secret"])
def test_hosted_or_credentialed_agent_url_rejected(url):
    cfg = local_config()
    cfg["llm"]["base_url"] = url
    with pytest.raises(ValueError, match="loopback"):
        capture_run.validate_local_config(cfg)


@pytest.mark.parametrize("url", ["http://localhost:8081/v1", "http://[::1]:8081/v1", "http://127.0.0.1:8000/v1"])
def test_loopback_agent_accepted(url):
    cfg = local_config()
    cfg["llm"]["base_url"] = url
    capture_run.validate_local_config(cfg)


def test_hosted_profile_rejected_even_with_loopback_url():
    cfg = local_config()
    cfg["profile"] = "kimi"
    with pytest.raises(ValueError, match="profile"):
        capture_run.validate_local_config(cfg)


def test_cached_revision_requires_weights_and_is_not_runtime_proof(tmp_path):
    repo = tmp_path / "models--test--model"
    (repo / "refs").mkdir(parents=True)
    (repo / "refs/main").write_text("abc123")
    snapshot = repo / "snapshots/abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    assert capture_run.cached_model("test/model", tmp_path)["cached_revision"] is None
    (snapshot / "model.safetensors").write_bytes(b"fixture")
    result = capture_run.cached_model("test/model", tmp_path)
    assert result["cached_revision"] == "abc123"
    assert result["runtime_loaded_revision"] is None


def make_inventory(tmp_path):
    bench = tmp_path / "benchmark.json"
    bench.write_text(json.dumps({"scenarios": [{"id": "dev_case"}]}))
    data = tmp_path / "data"
    for name in ("done", "missing", "excluded", "failed"):
        folder = data / name
        folder.mkdir(parents=True)
        (folder / "input.wav").write_bytes(b"independent fixture")
    (data / "done/result_test.json").write_text(json.dumps({"example_id": "dev_case", "status": "completed"}))
    (data / "failed/result_test.json").write_text(json.dumps({"example_id": "dev_case", "status": "inference_error"}))
    (data / "excluded/result_test.json").write_text(json.dumps({"example_id": "unknown_case"}))
    return data, bench


def test_inventory_discloses_missing_excluded_and_failed_results(tmp_path):
    data, bench = make_inventory(tmp_path)
    result = capture_run.result_inventory(data, bench, "test")
    assert result["expected_inputs"] == 4
    assert result["produced_result_files"] == 3
    assert result["evaluator_eligible_files"] == 2
    assert result["missing_results"] == ["missing"]
    assert result["excluded_count"] == 1
    assert result["statuses"]["inference_error"] == 1


def test_inventory_marks_malformed_files_without_mutating_them(tmp_path):
    data, bench = make_inventory(tmp_path)
    path = data / "missing/result_test.json"
    path.write_text("broken json")
    result = capture_run.result_inventory(data, bench, "test")
    assert result["excluded_count"] == 2
    assert path.read_text() == "broken json"


def test_capture_does_not_store_environment_secrets(tmp_path, monkeypatch):
    data, bench = make_inventory(tmp_path)
    v3 = tmp_path / "bench/Full-Duplex-Bench/v3"
    v3.mkdir(parents=True)
    (v3 / "benchmark_data_v2.json").write_bytes(bench.read_bytes())
    monkeypatch.setattr(capture_run, "ROOT", tmp_path)
    monkeypatch.setattr(capture_run, "git_state", lambda _: {"commit": "fixture"})
    monkeypatch.setattr(capture_run, "asr_mode", lambda: {"model": "test/asr", "adapted": True})
    monkeypatch.setenv("LIVEKIT_API_SECRET", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("PRISM_PROFILE", "local-mac")
    result = capture_run.capture(local_config(), data, "test", ["fixture-command"])
    assert "must-not-leak" not in json.dumps(result)
    assert result["environment"]["PRISM_PROFILE"] == "local-mac"
    assert result["models"]["llm"]["runtime_loaded_revision"] is None


@pytest.mark.parametrize("override, enabled, forced_mode", [
    (None, True, None), ("0", False, None), ("1", True, None),
    ("0", True, "car"), ("0", True, "assistant"),
])
def test_capture_result_speech_matches_effective_runtime_config(tmp_path, monkeypatch, override, enabled, forced_mode):
    from dataclasses import asdict
    from agent.config import make_coordinator_config
    data, bench = make_inventory(tmp_path)
    v3 = tmp_path / "bench/Full-Duplex-Bench/v3"
    v3.mkdir(parents=True)
    (v3 / "benchmark_data_v2.json").write_bytes(bench.read_bytes())
    monkeypatch.setattr(capture_run, "ROOT", tmp_path)
    monkeypatch.setattr(capture_run, "git_state", lambda _: {"commit": "fixture"})
    monkeypatch.setattr(capture_run, "asr_mode", lambda: {"model": "test/asr", "adapted": True})
    monkeypatch.delenv("MODE", raising=False)
    if forced_mode is not None:
        monkeypatch.setenv("MODE", forced_mode)
    monkeypatch.delenv("PRISM_BENCH_SPEAK_RESULTS", raising=False)
    if override is not None:
        monkeypatch.setenv("PRISM_BENCH_SPEAK_RESULTS", override)
    cfg = local_config()
    result = capture_run.capture(cfg, data, "test", ["fixture-command"])
    effective = result["effective_benchmark"]
    assert effective["mode"] == (forced_mode or "bench")
    assert effective["coordinator"] == asdict(make_coordinator_config(cfg, mode=forced_mode or "bench"))
    assert effective["coordinator"]["speak_results"] is enabled
    assert effective["result_speech_silence_ablation"] is (not enabled)
    assert result["environment"].get("PRISM_BENCH_SPEAK_RESULTS") == override
    assert result["configuration"]["coordinator"]["speak_results"] is True


def test_exact_evaluation_records_actual_denominator_and_preserves_outputs(tmp_path, monkeypatch):
    data, bench = make_inventory(tmp_path)
    v3 = tmp_path / "v3"
    v3.mkdir()
    (v3 / "benchmark_data_v2.json").write_bytes(bench.read_bytes())
    monkeypatch.setattr(fdb_eval, "V3", v3)
    monkeypatch.setattr(fdb_eval, "ROOT", tmp_path)
    monkeypatch.setenv("JUDGE", "none")
    monkeypatch.setattr(fdb_eval, "_judge_client", lambda: pytest.fail("exact mode must not call a judge"))
    originals = {p: p.read_bytes() for p in data.rglob("*.json")}

    def fake_main():
        out = Path(sys.argv[sys.argv.index("--output") + 1])
        out.write_text(json.dumps({"total_scenarios": 2}))

    for name in ("evaluate_pass_rate", "evaluate_tool_calls"):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(__name__=name, __file__=name + ".py", main=fake_main))
    output = tmp_path / "report"
    monkeypatch.setattr(sys, "argv", ["fdb_eval.py", "--provider", "test", "--results-dir", str(data), "--out", str(output)])
    fdb_eval.main()
    inventory = json.loads((output / "evaluation_inventory.json").read_text())
    judge = json.loads((output / "judge.json").read_text())
    assert inventory["expected_inputs"] == 4 and inventory["evaluated_files"] == 2
    assert judge["evaluation_complete"] is True and judge["requests"] == 0
    assert {p: p.read_bytes() for p in originals} == originals


def test_judge_request_errors_are_observable():
    metadata = {"requests": 0, "request_errors": 0, "returned_models": []}

    def fail(**kwargs):
        raise ConnectionError("unavailable")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
    judge = fdb_eval._ObservedJudge(client, metadata)
    with pytest.raises(ConnectionError):
        judge.create(model="fixture")
    assert metadata["requests"] == metadata["request_errors"] == 1


def test_adapted_judge_closes_underlying_client():
    closed = []
    client = SimpleNamespace(close=lambda: closed.append(True))
    fdb_eval._ModelSwap(client, "independent-test-model", {}).close()
    assert closed == [True]
