"""Summarize all declared loop trials, retaining the superseded candidate."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    conditions = {}
    for name in ["baseline", "candidate", "final"]:
        path = BASE / f"slow-io-{name}/report.json"
        data = json.loads(path.read_text())
        rows = data["runs"]
        valid = [row for row in rows if row["exit_code"] == 0 and "result" in row]
        phases = {}
        for phase in ["start", "finish"]:
            results = [row["result"] for row in valid if row["phase"] == phase]
            ms = [result["max_loop_lateness_seconds"] * 1000 for result in results]
            phases[phase] = {"attempted": sum(row["phase"] == phase for row in rows),
                             "completed": len(results), "max_loop_lateness_ms": ms,
                             "median_max_loop_lateness_ms": statistics.median(ms) if ms else None,
                             "largest_loop_lateness_ms": max(ms) if ms else None,
                             "run_seconds": [result["run_seconds"] for result in results],
                             "effects": sum(result["effects"] for result in results),
                             "all_outcomes_and_rows_done": len(results) == sum(row["phase"] == phase for row in rows) > 0 and all(
                                 result["outcome"] == "done" and result["drained"]
                                 and len(result["rows"]) == 1 and result["rows"][0]["call"]["status"] == "done"
                                 and len(result["injected_sleep_seconds"]) == 1
                                 for result in results)}
        conditions[name] = {"report_sha256": digest(path), "source_sha256": data["source_sha256"],
                            "helper_sha256": data["helper_sha256"],
                            "protocol_sha256": data["protocol_sha256"], "phases": phases}
    sources = ["agent/coordinator/executor.py", "agent/coordinator/coordinator.py",
               "agent/coordinator/ledger.py", "agent/telemetry/fdb_logger.py",
               "tests/unit/test_async_dispatch_telemetry.py"]
    result = {"analyzed_at_utc": datetime.now(timezone.utc).isoformat(), "conditions": conditions,
              "current_source_sha256": {name: digest(ROOT / name) for name in sources},
              "final_experiment_matches_current_executor": conditions["final"]["source_sha256"] == digest(ROOT / sources[0]),
              "all_experiments_used_same_helper": len({row["helper_sha256"] for row in conditions.values()}) == 1,
              "all_experiments_used_same_protocol": len({row["protocol_sha256"] for row in conditions.values()}) == 1,
              "limitations": ["Each value is the worst ticker lateness within one trial, then summarized across five trials per phase; not conversational latency.",
                              "Fixed condition and phase order, no machine background-load isolation. Regression checks overlapped portions of measurement; no statistical generalization.",
                              "The first candidate had a separately exposed cache race and is retained, not the final implementation.",
                              "Fresh subprocesses, mocked backend, no RTC, STT, LLM, TTS or physical audio. The required disk wait remains part of dispatch/completion time."]}
    (BASE / "loop-comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({name: {phase: row["median_max_loop_lateness_ms"] for phase, row in condition["phases"].items()}
                      for name, condition in conditions.items()}, indent=2))


if __name__ == "__main__":
    main()
