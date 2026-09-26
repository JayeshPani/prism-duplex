"""Derive host-boundary STT timings and exact input identity, without inference."""
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIAGNOSTICS = BASE / "local-stack-run1/stt-diagnostics"


def milliseconds(row, start, end):
    if start not in row or end not in row:
        return None
    return (row[end]["monotonic_ns"] - row[start]["monotonic_ns"]) / 1e6


def main():
    output = BASE / "stt-stage-analysis.json"
    assert not output.exists(), "Refuse to overwrite analysis."
    diagnostic_path = DIAGNOSTICS / "report.json"
    diagnostic = json.loads(diagnostic_path.read_text())
    suite = json.loads((BASE / "run-report.json").read_text())
    rooms = {}
    for run in suite["runs"]:
        capture = BASE / run["name"] / "report.json"
        if capture.exists():
            data = json.loads(capture.read_text())
            rooms[data["room"]] = run["name"]
    hashes, grouped, calls = {}, defaultdict(list), []
    for call in diagnostic["calls"]:
        stages = [{"name": stage["name"], "status": stage.get("status"),
                   "milliseconds": milliseconds(stage, "start", "end")}
                  for stage in call["stages"]]
        record = {"call_id": call["call_id"], "request_id": call["request_id"],
                  "trial": rooms.get(call["room"]), "room": call["room"],
                  "call_type": call["call_type"],
                  "caller_outcome": call.get("caller_return"),
                  "native_outcome": call.get("native_completion"),
                  "future_outcome": call.get("native_future_done"),
                  "milliseconds": {
                      "api_entry_to_submit": milliseconds(call, "engine_api_entry", "submitted"),
                      "submit_to_worker_entry": milliseconds(call, "submitted", "worker_entry"),
                      "worker_entry_to_native_completion": milliseconds(call, "worker_entry", "native_completion"),
                      "native_completion_to_caller_return": milliseconds(call, "native_completion", "caller_return"),
                      "api_entry_to_caller_return": milliseconds(call, "engine_api_entry", "caller_return"),
                  },
                  "stages": stages,
                  "submitted_equals_written_pcm": call["submitted_equals_written_pcm"]}
        for label in ("submitted_pcm", "written_wav_pcm"):
            data = call.get(label)
            record[label] = data
            if data and data.get("file"):
                path = DIAGNOSTICS / data["file"]
                actual = sha256(path.read_bytes()).hexdigest()
                assert actual == data["sha256"], path
                hashes[str(path.relative_to(BASE))] = actual
                if label == "submitted_pcm":
                    grouped[actual].append(call["call_id"])
        calls.append(record)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Every observed engine call, including prewarm, failures and cancellations.",
        "source_sha256": {str(path.relative_to(BASE)): sha256(path.read_bytes()).hexdigest()
                          for path in (Path(__file__), diagnostic_path, BASE / "run-report.json")},
        "calls": calls, "same_submitted_pcm_groups": list(grouped.values()),
        "verified_pcm_sha256": hashes,
        "diagnostic_health": {key: diagnostic[key] for key in (
            "source_unchanged", "dropped_calls", "dropped_requests", "retained_pcm_bytes",
            "observation_error_count", "observation_errors", "pending_native_call_ids",
            "observation_hooks", "observation_total_ns", "observation_max_ns")},
        "limits": [
            "Submit-to-entry includes instrumentation/dispatch overhead and worker scheduling, not only contention.",
            "Cancelled callers can return before native completion; negative delivery intervals retain this ordering.",
            "Stages nest. Do not sum model_transcribe with its load_audio/get_logmel/generate children.",
            "No accelerator synchronization added; deferred work may be charged to generate. These are host-call wall times, not exclusive GPU/CPU service times.",
            "Exact normalized PCM equality is a separate check from matching input WAVs or rounded segment duration.",
            "No causal historical latency improvement or explanation for an earlier uninstrumented call is asserted.",
        ],
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    for row in calls:
        print(json.dumps({key: row[key] for key in (
            "call_id", "trial", "call_type", "milliseconds", "stages")}), flush=True)


if __name__ == "__main__":
    main()
