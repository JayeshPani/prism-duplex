"""Offline independent STT audit, adapted from iteration12; run after suite exit."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
DIAG = BASE / "local-stack-run1/stt-diagnostics"
BOUNDARIES = {
    "api_entry_to_submit": ("engine_api_entry", "submitted"),
    "submit_to_worker_entry": ("submitted", "worker_entry"),
    "worker_entry_to_native_completion": ("worker_entry", "native_completion"),
    "native_completion_to_caller_return": ("native_completion", "caller_return"),
    "api_entry_to_caller_return": ("engine_api_entry", "caller_return"),
}
STAGES = ["engine_load_check", "tempfile_create", "wave_open", "wave_writeframes", "wave_close",
          "model_transcribe", "parakeet_load_audio", "parakeet_get_logmel", "parakeet_generate", "tempfile_unlink"]


def main():
    output = BASE / "stt-stage-independent-audit.json"
    assert not output.exists(), "Refuse to overwrite audit."
    receipts, issues = {}, []

    def read(path):
        data = path.read_bytes()
        receipts[str(path.relative_to(BASE))] = sha256(data).hexdigest()
        return json.loads(data)

    def check(condition, message):
        if not condition:
            issues.append(message)

    raw = read(DIAG / "report.json")
    suite = read(BASE / "run-report.json")
    assert suite.get("finished_at"), "Suite must have exited before audit."
    protocol = read(BASE / "protocol.json")
    preflight = read(BASE / "preflight.json")
    exit_record = read(BASE / "local-stack-run1/stt-diagnostics-exit.json")
    comparable = None
    if (BASE / "stt-stage-analysis.json").exists():
        comparable = {r["call_id"]: r for r in read(BASE / "stt-stage-analysis.json")["calls"]}
    check([r["case"] for r in suite["runs"]] == protocol["run_order"], "Run declaration differs.")
    requests = {r["request_id"]: r for r in raw["requests"]}
    check(len(requests) == len(raw["requests"]), "Duplicate request IDs.")
    check(len({c["call_id"] for c in raw["calls"]}) == len(raw["calls"]), "Duplicate call IDs.")
    rows, pcm_hashes, pcm_groups, calls_by_room = [], {}, defaultdict(list), defaultdict(list)
    stages_verified, retained_bytes = 0, 0
    for call in raw["calls"]:
        cid = call["call_id"]
        request = requests.get(call["request_id"])
        check(request is not None and call["room"] == request["room"] and call["call_type"] == request["call_type"], f"Origin mismatch: {cid}")
        calls_by_room[call["room"]].append(call)
        timings = {name: (call[end]["monotonic_ns"] - call[start]["monotonic_ns"]) / 1e6
                   if start in call and end in call else None for name, (start, end) in BOUNDARIES.items()}
        if comparable is not None:
            check(cid in comparable and timings == comparable[cid]["milliseconds"], f"Independent boundary calculation differs: {cid}")
        intervals, stages = {}, []
        for stage in call["stages"]:
            elapsed = None
            if "end" in stage:
                start, end = (stage[k]["monotonic_ns"] for k in ("start", "end"))
                elapsed = (end - start) / 1e6
                check(end >= start, f"Negative stage: {cid}/{stage['name']}")
                check(abs(elapsed / 1000 - stage["wall_seconds"]) < 1e-12, f"Stored duration differs: {cid}/{stage['name']}")
                if "worker_entry" in call and "native_completion" in call:
                    check(call["worker_entry"]["monotonic_ns"] <= start <= end <= call["native_completion"]["monotonic_ns"], f"Stage outside native bounds: {cid}/{stage['name']}")
                intervals[stage["name"]] = (start, end)
                stages_verified += 1
            stages.append({"name": stage["name"], "status": stage.get("status", "pending"), "milliseconds": elapsed})
        if comparable is not None:
            check(stages == comparable.get(cid, {}).get("stages"), f"Independent stage calculation differs: {cid}")
        if call.get("native_completion", {}).get("status") == "returned":
            check([s["name"] for s in stages] == STAGES, f"Unexpected returned-call stages: {cid}")
            outer = [n for n in STAGES if not n.startswith("parakeet_")]
            if all(n in intervals for n in STAGES):
                check(all(intervals[a][1] <= intervals[b][0] for a, b in zip(outer, outer[1:])), f"Outer stages overlap: {cid}")
                children = STAGES[6:9]
                check(all(intervals[a][1] <= intervals[b][0] for a, b in zip(children, children[1:])), f"Child stages overlap: {cid}")
                check(intervals["model_transcribe"][0] <= intervals[children[0]][0] <= intervals[children[-1]][1] <= intervals["model_transcribe"][1], f"Child nesting differs: {cid}")
        payloads = {}
        for label in ("submitted_pcm", "written_wav_pcm"):
            receipt = call.get(label, {})
            if not receipt.get("file"):
                continue
            data = (DIAG / receipt["file"]).read_bytes()
            digest = sha256(data).hexdigest()
            check(digest == receipt["sha256"], f"PCM hash differs: {cid}/{label}")
            check(len(data) == receipt["bytes"] == receipt["requested_bytes"], f"PCM size differs: {cid}/{label}")
            check(receipt["sample_rate"] == 16000 and len(data) / 32000 == receipt["duration_seconds"], f"PCM duration differs: {cid}/{label}")
            pcm_hashes[str((DIAG / receipt["file"]).relative_to(BASE))] = digest
            retained_bytes += len(data)
            payloads[label] = data
            if label == "submitted_pcm":
                pcm_groups[digest].append(cid)
        equality = payloads["submitted_pcm"] == payloads["written_wav_pcm"] if len(payloads) == 2 else None
        if equality is not None:
            check(equality and call["submitted_equals_written_pcm"] is True, f"Actual WAV PCM differs from submitted PCM: {cid}")
        if call["call_type"] == "warmup":
            check(call["room"] is None and payloads.get("submitted_pcm") == bytes(32000), f"Warmup identity differs: {cid}")
        rows.append({"call_id": cid, "request_id": call["request_id"], "room": call["room"], "call_type": call["call_type"],
                     "caller_outcome": call.get("caller_return"), "native_outcome": call.get("native_completion"),
                     "future_outcome": call.get("native_future_done"), "milliseconds": timings, "stages": stages,
                     "submitted_pcm": call.get("submitted_pcm"), "written_wav_pcm": call.get("written_wav_pcm"),
                     "submitted_equals_written_pcm": equality})
    check(retained_bytes == raw["retained_pcm_bytes"], "Retained byte counter differs.")
    mappings, known_rooms, counts = [], set(), Counter()
    for run in suite["runs"]:
        capture_path = BASE / run["name"] / "report.json"
        if not capture_path.exists():
            mappings.append({"trial": run["name"], "status": "capture_report_missing", "runner": run})
            counts["capture_report_missing"] += 1
            continue
        capture = read(capture_path)
        event_path = capture_path.with_name("events.jsonl")
        raw_events = event_path.read_bytes()
        receipts[str(event_path.relative_to(BASE))] = sha256(raw_events).hexdigest()
        events = [json.loads(line) for line in raw_events.splitlines()]
        finals = [e for e in events if e.get("event", {}).get("type") == "user_final"]
        calls = calls_by_room.get(capture["room"], [])
        known_rooms.add(capture["room"])
        published = [i for i in capture["inputs"] if i.get("timing", {}).get("published_samples", 0)]
        counts[capture["status"]] += 1
        turns = []
        for index in range(max(len(finals), len(calls), len(published))):
            call = calls[index] if index < len(calls) else None
            final = finals[index] if index < len(finals) else None
            input_row = published[index] if index < len(published) else None
            text_match = call.get("caller_return", {}).get("result_text") == final["event"]["data"]["text"] if call and final else None
            turns.append({"call_id": call["call_id"] if call else None,
                          "input_label": input_row["label"] if input_row else None,
                          "final": final["event"]["data"] if final else None, "text_matches_call": text_match})
        mappings.append({"trial": run["name"], "room": capture["room"], "status": capture["status"],
                         "failed_stage": capture.get("failed_stage"), "error": capture.get("error"),
                         "published_utterances": len(published), "recognition_calls": len(calls),
                         "user_finals": len(finals), "turns": turns})
    current_hashes = {p: sha256(Path(p).read_bytes()).hexdigest() for p in raw["source_sha256_before"]}
    source_match = current_hashes == raw["source_sha256_before"] == raw["source_sha256_after"]
    check(source_match and raw["source_unchanged"], "Diagnostic sources differ.")
    helper_same = (BASE / "stt_diagnostics.py").read_bytes() == (BASE.parent / "iteration12/stt_diagnostics.py").read_bytes()
    check(helper_same, "STT observer differs from iteration12 reviewed helper.")
    check(exit_record.get("diagnostics_saved"), "Diagnostic flush did not complete.")
    check(not (set(calls_by_room) - known_rooms - {None}), "Native calls contain an unmapped room.")
    recognition = [r for r in rows if r["call_type"] == "_recognize_impl"]
    timed = [r for r in recognition if r["milliseconds"]["api_entry_to_caller_return"] is not None]
    slowest = max(timed, key=lambda r: r["milliseconds"]["api_entry_to_caller_return"], default=None)
    call_request_ids = {c["request_id"] for c in raw["calls"]}
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Every retained call/stage/PCM and every declared capture, preserving failure, cancellation and pending outcomes.",
              "source_sha256": {**receipts, "audit_stt_stages.py": sha256(Path(__file__).read_bytes()).hexdigest()},
              "artifact_consistency_checks_pass": not issues, "consistency_issues": issues,
              "counts": {"declared_captures": len(protocol["run_order"]), "capture_statuses": dict(counts),
                         "native_calls": len(rows), "recognition_calls": len(recognition),
                         "call_types": dict(Counter(c["call_type"] for c in raw["calls"])),
                         "request_types": dict(Counter(r["call_type"] for r in raw["requests"])),
                         "request_outcomes": dict(Counter(r.get("return", {}).get("status", "pending") for r in raw["requests"])),
                         "transcribe_since_without_engine_call": sum(r["call_type"] == "transcribe_since" and r["request_id"] not in call_request_ids for r in raw["requests"]),
                         "verified_stages": stages_verified, "verified_pcm_files": len(pcm_hashes), "distinct_submitted_pcm_hashes": len(pcm_groups)},
              "capture_mapping": mappings, "calls": rows, "slowest_recognition_call": slowest,
              "verified_pcm_sha256": pcm_hashes, "same_submitted_pcm_groups": list(pcm_groups.values()),
              "source_current_before_after_match": source_match, "observer_identical_to_iteration12": helper_same,
              "native_model_references": [dict(kind=k, model=m, revision=r) for k, m, r in sorted({(c["model_kind"], c["model_name"], c["revision"]) for c in raw["calls"]})],
              "preflight_model_receipts": preflight["model_files"],
              "diagnostic_health": {k: raw[k] for k in ("dropped_calls", "dropped_requests", "observation_error_count",
                  "observation_errors", "pending_native_call_ids", "retained_pcm_bytes", "observation_hooks", "observation_total_ns", "observation_max_ns")},
              "dropped_stage_count": sum(r.get("dropped_stages", 0) for r in raw["calls"] + raw["requests"]),
              "limits": [
                  "Consistency checks are not a conversation success score. All declared failures remain in the capture mapping.",
                  "Mapping uses exact room plus chronological call/input/final order and transcript comparison; native diagnostics have no coordinator operation ID.",
                  "Exact normalized PCM equality is checked independently of reused source WAVs. Warmup remains separate from recognition counts.",
                  "Only completed stage endpoints permit duration computation; absent/cancelled/pending outcomes are retained rather than inferred successful.",
                  "Stage intervals nest. No extra accelerator synchronization was added; lazy frontend work can appear within generation host wall time.",
                  "Queue boundaries include dispatch/instrumentation/scheduling. Measured helper overhead excludes some clock/wrapper/context costs and is not a bound on total observer overhead.",
                  "Identical observer bytes preserve the previously reviewed wrapper behavior and its limits, not zero observer impact. Preflight model receipts are preserved; this audit does not reread GB-scale model files.",
                  "These observations neither explain the earlier uninstrumented 13-second call nor demonstrate causal performance improvement.",
              ]}
    with output.open("x") as handle:
        handle.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(output), "counts": result["counts"], "issues": issues}))


if __name__ == "__main__":
    main()
