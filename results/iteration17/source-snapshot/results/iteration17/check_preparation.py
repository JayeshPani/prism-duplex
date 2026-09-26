"""Verify only the declared cache-entry factor differs before inference."""
from datetime import datetime, timezone
from hashlib import sha256
import ast
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def main():
    target = BASE / "preparation-check.json"
    assert not target.exists()
    plan = read(BASE / "plan.json")
    baseline = ROOT / "results/iteration16"
    original_protocol = read(baseline / "protocol.json")
    original_command = read(baseline / "local-stack-run1/llm-launch.json")["command"]
    assert original_command.count("--prompt-cache-size") == 1
    index = original_command.index("--prompt-cache-size") + 1
    assert original_command[index] == "2"
    tested = read(baseline / "tested-source.json")
    assert all(digest(ROOT / name) == expected for name, expected in tested["paths_sha256"].items())
    method_sets, inputs, rows = [], {}, []
    for arm in plan["arms"]:
        directory = ROOT / arm["directory"]
        assert not (directory / "preflight.json").exists() and not (directory / "local-stack-run1").exists()
        protocol, arm_plan = read(directory / "protocol.json"), read(directory / "plan.json")
        assert protocol["cache_entries"] == arm["entries"]
        for key in ("expected", "run_order", "expected_totals", "protocol_limits"):
            assert protocol[key] == original_protocol[key], (arm["name"], key)
        assert set(protocol["inputs"]) == set(original_protocol["inputs"])
        for name, item in protocol["inputs"].items():
            source = directory / "audio-assets" / f"{name}.wav"
            assert digest(source) == item["sha256"] == original_protocol["inputs"][name]["sha256"]
            inputs[str(source.relative_to(ROOT))] = digest(source)
        command = list(original_command)
        command[index] = str(arm["entries"])
        assert arm_plan["llm_command"] == command
        assert arm_plan["baseline_livekit_config_sha256"] == digest(baseline / "local-stack-run1/livekit.yaml")
        methods = {path.name: digest(path) for path in sorted(directory.glob("*.py"))}
        assert methods
        method_sets.append(methods)
        for path in directory.glob("*.py"):
            ast.parse(path.read_text(), filename=str(path))
        speech = read(directory / "received-audio-review-protocol.json")
        assert speech["script_sha256"] == methods["review_received_audio.py"]
        assert speech["suite_protocol_sha256"] == digest(directory / "protocol.json")
        assert speech["review"] == read(baseline / "received-audio-review-protocol.json")["review"]
        assert digest(directory / "tested-source.json") == digest(baseline / "tested-source.json")
        assert digest(directory / "full-tests.txt") == tested["log_sha256"]
        rows.append({"arm": arm["name"], "cache_entries": arm["entries"],
                     "command": command, "protocol_sha256": digest(directory / "protocol.json"),
                     "plan_sha256": digest(directory / "plan.json"), "unchanged_case_criteria": True})
    assert method_sets[0] == method_sets[1] == method_sets[2]
    assert [row["cache_entries"] for row in rows] == [2, 1, 2]
    source_paths = [Path(__file__), BASE / "plan.json", BASE / "comparison-protocol.json",
                    baseline / "protocol.json", baseline / "tested-source.json",
                    baseline / "local-stack-run1/livekit.yaml",
                    baseline / "local-stack-run1/llm-launch.json"]
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "status": "passed",
              "scope": "Static preparation checks only; no tests, model imports or services launched.",
              "arms": rows, "methods_byte_identical": True, "method_sha256": method_sets[0],
              "input_sha256": inputs, "inherited_tested_source_count": len(tested["paths_sha256"]),
              "source_sha256": {str(path.relative_to(ROOT)): digest(path) for path in source_paths}}
    with target.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print("All three arms preserve case criteria, source, methods and audio; only cache-entry argv differs.")


if __name__ == "__main__":
    main()
