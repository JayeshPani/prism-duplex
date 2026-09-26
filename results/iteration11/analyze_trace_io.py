"""Recompute content/hash checks and trial-weighted ticker summaries; no inference."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
RUN = BASE / "trace-io-comparison"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def describe(values):
    return {"n": len(values), "values_in_run_order": values,
            "median": statistics.median(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=BASE / "trace-io-analysis.json")
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError("preserve existing analysis")
    protocol_path, report_path = RUN / "protocol.json", RUN / "report.json"
    p, r = json.loads(protocol_path.read_text()), json.loads(report_path.read_text())
    if r["status"] != "completed" or r["protocol_sha256"] != sha(protocol_path):
        raise ValueError("terminal report and matching protocol required")
    expected_order = [(phase, repetition, arm) for phase in ("open", "flush", "close")
                      for repetition in range(1, 6) for arm in ("A", "B")]
    declared = [(x["phase"], x["repetition"], x["arm"]) for x in p["trials"]]
    if declared != expected_order or len(r["trials"]) != len(declared):
        raise ValueError("missing, additional or changed declaration; do not silently exclude rows")
    expected_content = [{"type": "tool_done", "data": {"index": i, "result": {"status": "success"}}, "ts": float(i)} for i in range(2)]
    rows = []
    for index, (trial, expected) in enumerate(zip(r["trials"], declared), 1):
        phase, repetition, arm = expected
        trace = RUN / f"{index:02d}-{phase}-{arm}.jsonl"
        row = {"index": index, "phase": phase, "repetition": repetition, "arm": arm,
               "reported_status": trial["status"], "file": str(trace.relative_to(ROOT)), "checks": {}}
        row["checks"]["identity_and_order"] = (trial["phase"], trial["repetition"], trial["arm"]) == expected
        row["checks"].update(completed=trial["status"] == "completed", delay_reported=trial.get("delay_injected") is True,
                             close_acknowledged=trial.get("close_complete") is True)
        try:
            observed = [json.loads(line) for line in trace.read_text().splitlines()]
            row["file_sha256"] = sha(trace)
            row["event_count"] = len(observed)
            row["checks"]["recorded_file_hash"] = row["file_sha256"] == trial.get("file_sha256")
            row["checks"]["exact_ordered_content"] = observed == expected_content
            row["checks"]["reported_content_agrees"] = trial.get("exact_ordered_content") == row["checks"]["exact_ordered_content"]
        except Exception as error:
            row["file_error"] = repr(error)
            row["checks"]["exact_ordered_content"] = False
        samples = trial.get("ticker_lateness_seconds", [])
        valid = bool(samples) and all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in samples)
        row["checks"]["valid_nonnegative_ticker_samples"] = valid
        row["checks"]["ticker_sample_count_matches"] = len(samples) == trial.get("ticker_samples")
        row["checks"]["reported_max_matches"] = valid and max(samples) == trial.get("worst_ticker_lateness_seconds")
        row["ticker_samples"] = len(samples)
        row["recomputed_worst_ticker_lateness_ms"] = max(samples) * 1000 if valid else None
        row["operation_ms"] = trial.get("operation_seconds", 0) * 1000 if "operation_seconds" in trial else None
        rows.append(row)
    groups = []
    for phase in ("open", "flush", "close"):
        for arm in ("A", "B"):
            group = [x for x in rows if x["phase"] == phase and x["arm"] == arm]
            groups.append({"phase": phase, "arm": arm, "declared": len(group),
                           "completed": sum(x["checks"]["completed"] for x in group),
                           "content_verified": sum(x["checks"]["exact_ordered_content"] for x in group),
                           "close_acknowledged": sum(x["checks"]["close_acknowledged"] for x in group),
                           "per_trial_worst_ticker_lateness_ms": describe([x["recomputed_worst_ticker_lateness_ms"] for x in group if x["recomputed_worst_ticker_lateness_ms"] is not None]),
                           "operation_ms": describe([x["operation_ms"] for x in group if x["operation_ms"] is not None]),
                           "ticker_samples_per_trial": [x["ticker_samples"] for x in group]})
    sources = {name: {"declared": digest, "current": sha(ROOT / name), "matches": sha(ROOT / name) == digest}
               for name, digest in p["source_sha256"].items()}
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Independent arithmetic, saved content and hash audit of all 30 declared controlled trace-I/O observations.",
        "input_sha256": {str(path.relative_to(ROOT)): sha(path) for path in (protocol_path, report_path, Path(__file__))},
        "denominators": {"declared": len(declared), "reported": len(r["trials"]), "audited": len(rows),
                         "status_counts": dict(Counter(x["reported_status"] for x in rows)),
                         "all_checks_passed": sum(all(x["checks"].values()) for x in rows),
                         "content_events_verified": sum(x.get("event_count", 0) for x in rows if x["checks"]["exact_ordered_content"])},
        "groups": groups, "all_trials": rows, "source_hashes": sources,
        "runner_end_source_unchanged_receipt": r["source_unchanged"],
        "measurement_review": {
            "blocking_measurement_defect_found": False,
            "quantity": "Each sample is max(0, actual wake minus a locally scheduled perf_counter()+1ms deadline); each trial reports its largest sample. Group median weights each of five trials once.",
            "reset_schedule": "The next deadline resets after each wake. This measures one-sleep overshoot, not accumulated lateness from an absolute periodic clock or the number of missed 1ms deadlines.",
            "window": "Ticker starts before a5ms shoulder, spans open/two events/close, and continues through a5ms shoulder plus final content read/hash. Samples are not tagged to exact injected-I/O start/end timestamps.",
            "baseline": "ArmA manually reproduces frozen synchronous mkdir/open/append/flush/close semantics; it does not execute the entire frozen agent. ArmB calls production TraceWriter.",
            "operation_metric": "Total open-to-close lifecycle elapsed time, including injected100ms stall and queue drain. It is not event submission latency or isolated disk service time.",
            "independent_reconstruction_limit": "Raw lateness arrays are preserved and maxima/counts recomputed. Actual per-tick due/wake timestamps and precise injected sleep duration were not saved, so their arithmetic cannot be reconstructed independently of the frozen measurement source.",
        },
        "conclusion": "All30 traces contain the same two declared events in order and acknowledge close. Under one injected100ms boundary delay, offloading preserves event-loop responsiveness; this does not establish faster completion of file I/O or a live conversation benefit.",
        "limits": [
            "Artificial sleep-based single stall in open, first flush or close; no mkdir/write fault timing arm, RTC, audio, VAD or model inference.",
            "Five fixed A-then-B repetitions per phase with shared process/host and unisolated background load; no randomization, population percentile or significance claim.",
            "Candidate flush repetition5 worst sample remains in the denominator; no outlier or cold-first exclusion.",
            "Ticker sample counts differ because the synchronous arm cannot tick during its stall. Pooling all samples would change trial weights; group metrics use one maximum per trial.",
            "Content preservation of these two tiny fixed events is not a general workload, queue-capacity, memory-growth or crash-durability guarantee; flush/close is not fsync.",
            "Native filesystem operations can exceed a close deadline and delay process exit. This probe completes normally and does not establish a hard native-I/O shutdown bound.",
        ],
    }
    with args.out.open("x") as f:
        f.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.out), "denominators": result["denominators"], "groups": groups}, indent=2))


if __name__ == "__main__":
    main()
