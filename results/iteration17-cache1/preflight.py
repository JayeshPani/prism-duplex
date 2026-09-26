"""Verify one declared cache-entry arm before any service is launched."""
from datetime import datetime, timezone
import ast
import hashlib
import json
from pathlib import Path
import socket

import psutil

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    output = BASE / "preflight.json"
    assert not output.exists(), "Refuse to replace preflight evidence."
    assert not (BASE / "local-stack-run1").exists(), "Suite already started."
    tested = json.loads((BASE / "tested-source.json").read_text())
    assert tested["exit_code"] == 0 and tested["unchanged_during_test"]
    protocol = json.loads((BASE / "protocol.json").read_text())
    plan = json.loads((BASE / "plan.json").read_text())
    baseline_launch = ROOT / "results/iteration16/local-stack-run1/llm-launch.json"
    assert digest(baseline_launch) == plan["baseline_llm_launch_sha256"]
    baseline_argv = json.loads(baseline_launch.read_text())["command"]
    assert baseline_argv.count("--prompt-cache-size") == 1
    assert type(protocol["cache_entries"]) is int and protocol["cache_entries"] in {1, 2}
    expected_argv = list(baseline_argv)
    expected_argv[expected_argv.index("--prompt-cache-size") + 1] = str(protocol["cache_entries"])
    assert plan["llm_command"] == expected_argv, "Only the declared cache-entry count may change"
    baseline_livekit = ROOT / "results/iteration16/local-stack-run1/livekit.yaml"
    assert digest(baseline_livekit) == plan["baseline_livekit_config_sha256"]
    assert digest(BASE / "tested-source.json") == digest(ROOT / "results/iteration16/tested-source.json")
    assert digest(BASE / "full-tests.txt") == tested["log_sha256"]
    assert digest(BASE / "full-tests.txt") == digest(ROOT / "results/iteration16/full-tests.txt")
    assert plan["inherited_tests"]["rerun"] is False
    assert all(digest(BASE / "audio-assets" / f"{name}.wav") == row["sha256"]
               for name, row in protocol["inputs"].items())
    assert all(digest(ROOT / name) == value for name, value in tested["paths_sha256"].items())
    method = json.loads((BASE / "received-audio-review-protocol.json").read_text())
    assert method["script_sha256"] == digest(BASE / "review_received_audio.py")
    assert method["suite_protocol_sha256"] == digest(BASE / "protocol.json")
    required = ["instrumented_worker.py", "stt_diagnostics.py"]
    for name in required:
        assert (BASE / name).is_file(), name
    for path in BASE.rglob("*.py"):
        ast.parse(path.read_text(), filename=str(path))
    previous = json.loads((ROOT / "results/iteration16/post-inference-validation.json").read_text())
    models = {name: {"reference": row["expected"], "observed": digest(name)}
              for name, row in previous["model_files"].items()}
    assert len(models) == 10
    assert all(row["reference"] == row["observed"] for row in models.values())
    for port, kind in [(7880, socket.SOCK_STREAM), (7882, socket.SOCK_DGRAM),
                       (8081, socket.SOCK_STREAM), (8082, socket.SOCK_STREAM)]:
        with socket.socket(socket.AF_INET, kind) as check:
            check.bind(("127.0.0.1", port))
    sources = [*sorted((ROOT / "agent").rglob("*.py")), ROOT / "agent/config.yaml",
               *sorted(BASE.glob("*.py")), BASE / "protocol.json", BASE / "plan.json",
               BASE / "received-audio-review-protocol.json", BASE / "tested-source.json", BASE / "full-tests.txt",
               ROOT / "results/iteration11/instrumented_worker.py", baseline_launch, baseline_livekit,
               ROOT / "results/iteration17/comparison-protocol.json",
               ROOT / "scripts/local_livekit_audio_experiment.py",
               ROOT / "scripts/local_livekit_worker.py",
               *sorted((BASE / "audio-assets").glob("*.wav"))]
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "status": "passed",
              "production_tested_source_files_unchanged": len(tested["paths_sha256"]),
              "arm_id": protocol["arm_id"], "cache_entries": protocol["cache_entries"],
              "inherited_test_receipt": plan["inherited_tests"],
              "model_files": models,
              "source_input_method_sha256": {str(p): digest(p) for p in sources},
              "memory": psutil.virtual_memory()._asdict(), "swap": psutil.swap_memory()._asdict(),
              "limits": "Reference-byte matches freeze observed assets, not publisher provenance. Instrumentation is experiment-only and adds overhead. No service has launched in this preflight."}
    with output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print("Preflight passed: current source, methods, inputs and ten model references verified; ports available.")


if __name__ == "__main__":
    main()
