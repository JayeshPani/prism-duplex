"""Independent arithmetic, PCM and capture audit; no inference or model imports."""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIAG = BASE / "local-stack-run1/stt-diagnostics"


def main():
    output = BASE / "stt-stage-independent-audit.json"
    assert not output.exists(), "Refuse to overwrite audit."
    receipts = {}

    def read(path):
        raw = path.read_bytes()
        receipts[str(path.relative_to(BASE))] = sha256(raw).hexdigest()
        return json.loads(raw)

    raw = read(DIAG / "report.json")
    derived = read(BASE / "stt-stage-analysis.json")
    suite = read(BASE / "run-report.json")
    exit_record = read(BASE / "local-stack-run1/stt-diagnostics-exit.json")
    expected_names = ["rtc-waypoint-1", "rtc-baseline-1", "rtc-waypoint-2", "rtc-correction-1"]
    assert [r["name"] for r in suite["runs"]] == expected_names
    captures, calls_by_room, mappings = {}, {}, []
    for run in suite["runs"]:
        capture = read(BASE / run["name"] / "report.json")
        path = BASE / run["name"] / "events.jsonl"
        event_bytes = path.read_bytes()
        receipts[str(path.relative_to(BASE))] = sha256(event_bytes).hexdigest()
        events = [json.loads(line) for line in event_bytes.splitlines()]
        finals = [e for e in events if e.get("event", {}).get("type") == "user_final"]
        captures[capture["room"]] = (run, capture, finals, events)
    requests = {r["request_id"]: r for r in raw["requests"]}
    assert len(requests) == len(raw["requests"]) == 14
    assert Counter(r["call_type"] for r in requests.values()) == {
        "load": 1, "warmup": 1, "_recognize_impl": 6, "transcribe_since": 6}
    assert all(r["return"]["status"] == "returned" for r in requests.values())
    assert all(r["entry"]["monotonic_ns"] <= r["return"]["monotonic_ns"] for r in requests.values())
    comparable = {c["call_id"]: c for c in derived["calls"]}
    verified_pcm, rows = {}, []
    boundaries = {
        "api_entry_to_submit": ("engine_api_entry", "submitted"),
        "submit_to_worker_entry": ("submitted", "worker_entry"),
        "worker_entry_to_native_completion": ("worker_entry", "native_completion"),
        "native_completion_to_caller_return": ("native_completion", "caller_return"),
        "api_entry_to_caller_return": ("engine_api_entry", "caller_return"),
    }
    expected_stages = ["engine_load_check", "tempfile_create", "wave_open", "wave_writeframes",
                       "wave_close", "model_transcribe", "parakeet_load_audio", "parakeet_get_logmel",
                       "parakeet_generate", "tempfile_unlink"]
    assert len(raw["calls"]) == len(comparable) == 7
    for call in raw["calls"]:
        request = requests[call["request_id"]]
        assert call["room"] == request["room"] and call["call_type"] == request["call_type"]
        calls_by_room.setdefault(call["room"], []).append(call)
        ns = lambda key: call[key]["monotonic_ns"]
        timings = {key: (ns(end) - ns(start)) / 1e6 for key, (start, end) in boundaries.items()}
        assert timings == comparable[call["call_id"]]["milliseconds"]
        assert all(value >= 0 for value in timings.values())
        assert abs(sum(list(timings.values())[:4]) - timings["api_entry_to_caller_return"]) < 1e-8
        assert request["entry"]["monotonic_ns"] <= ns("engine_api_entry") <= ns("caller_return") <= request["return"]["monotonic_ns"]
        assert all(call[k]["status"] == "returned" for k in ("caller_return", "native_completion", "native_future_done"))
        assert not call["native_future_done"]["cancelled"]
        assert call["caller_return"]["result_text"] == call["native_completion"]["result_text"]
        assert [s["name"] for s in call["stages"]] == expected_stages
        stages = {}
        intervals = {}
        for stage, reported in zip(call["stages"], comparable[call["call_id"]]["stages"]):
            start, end = (stage[k]["monotonic_ns"] for k in ("start", "end"))
            assert ns("worker_entry") <= start <= end <= ns("native_completion")
            assert stage["status"] == reported["status"] == "returned"
            assert abs((end - start) / 1e9 - stage["wall_seconds"]) < 1e-12
            stages[stage["name"]] = (end - start) / 1e6
            intervals[stage["name"]] = (start, end)
            assert stages[stage["name"]] == reported["milliseconds"]
        outer = [n for n in expected_stages if not n.startswith("parakeet_")]
        assert all(intervals[a][1] <= intervals[b][0] for a, b in zip(outer, outer[1:]))
        children = expected_stages[6:9]
        assert all(intervals[a][1] <= intervals[b][0] for a, b in zip(children, children[1:]))
        assert intervals["model_transcribe"][0] <= intervals[children[0]][0] <= intervals[children[-1]][1] <= intervals["model_transcribe"][1]
        assert call["stages"][0]["model_present_before"] is True
        pcm = []
        for label in ("submitted_pcm", "written_wav_pcm"):
            receipt = call[label]
            data = (DIAG / receipt["file"]).read_bytes()
            digest = sha256(data).hexdigest()
            assert receipt["status"] == "retained" and receipt["sample_rate"] == 16000
            assert len(data) == receipt["bytes"] == receipt["requested_bytes"]
            assert len(data) / 32000 == receipt["duration_seconds"]
            assert digest == receipt["sha256"] == comparable[call["call_id"]][label]["sha256"]
            verified_pcm[str((DIAG / receipt["file"]).relative_to(BASE))] = digest
            pcm.append(data)
        assert pcm[0] == pcm[1] and call["submitted_equals_written_pcm"] is True
        if call["call_type"] == "warmup":
            assert call["room"] is None and pcm[0] == bytes(32000)
        rows.append({"call_id": call["call_id"], "request_id": call["request_id"],
                     "room": call["room"], "call_type": call["call_type"],
                     "text": call["caller_return"]["result_text"], "milliseconds": timings,
                     "stage_milliseconds": stages, "pcm_bytes": len(pcm[0]),
                     "pcm_sha256": sha256(pcm[0]).hexdigest()})
    assert verified_pcm == derived["verified_pcm_sha256"]
    assert sum((DIAG / c[k]["file"]).stat().st_size for c in raw["calls"] for k in ("submitted_pcm", "written_wav_pcm")) == raw["retained_pcm_bytes"]
    assert len({r["pcm_sha256"] for r in rows}) == 7
    for room, (run, capture, finals, events) in captures.items():
        room_calls = calls_by_room.get(room, [])
        published = [i for i in capture["inputs"] if i.get("timing", {}).get("published_samples", 0)]
        if run["name"] == "rtc-baseline-1":
            assert capture["status"] == "failed" and run["exit_code"] == 1
            assert capture["failed_stage"] == "connect_and_session_ready" and capture["error"] == "TimeoutError()"
            assert not room_calls and not finals and not published
            assert not any(e["kind"] in ("input_started", "input_frame_queued") for e in events)
        else:
            assert capture["status"] == "completed_observation" and run["exit_code"] == 0
            assert len(room_calls) == len(finals) == len(published) == 2
        turns = []
        for call, final, input_row in zip(room_calls, finals, published):
            assert final["event"]["data"]["text"] == call["caller_return"]["result_text"]
            assert input_row["timing"]["started_monotonic"] <= call["engine_api_entry"]["monotonic_ns"] / 1e9
            assert call["caller_return"]["monotonic_ns"] / 1e9 <= final["received_monotonic"]
            turns.append({"call_id": call["call_id"], "intent_version": final["event"]["data"]["intent_version"],
                          "input_label": input_row["label"], "text": final["event"]["data"]["text"]})
        mappings.append({"trial": run["name"], "room": room, "capture_status": capture["status"],
                         "published_utterances": len(published), "recognition_calls": len(room_calls), "turns": turns})
    call_request_ids = {c["request_id"] for c in raw["calls"]}
    assert all(r["request_id"] not in call_request_ids and r["return"]["result_text"] is None
               for r in requests.values() if r["call_type"] == "transcribe_since")
    assert raw["source_sha256_before"] == raw["source_sha256_after"] and raw["source_unchanged"]
    assert all(sha256(Path(p).read_bytes()).hexdigest() == h for p, h in raw["source_sha256_before"].items())
    assert raw["dropped_calls"] == raw["dropped_requests"] == raw["observation_error_count"] == 0
    assert not raw["pending_native_call_ids"] and not raw["observation_errors"]
    assert exit_record["diagnostics_saved"] and exit_record["exception"] == "SystemExit(0)"
    slowest = max((r for r in rows if r["room"]), key=lambda r: r["milliseconds"]["api_entry_to_caller_return"])
    report = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Independent recomputation of every recorded STT boundary and stage; all declared captures retained.",
              "source_sha256": {**receipts, "audit_stt_stages.py": sha256(Path(__file__).read_bytes()).hexdigest()},
              "all_checks_pass": True,
              "counts": {"declared_captures": 4, "completed_captures": 3, "failed_before_input": 1,
                         "recognition_calls": 6, "warmup_calls": 1, "local_stt_requests": 14,
                         "transcribe_since_without_inference": 6, "verified_stages": 70, "verified_pcm_files": 14},
              "capture_mapping": mappings, "calls": rows, "verified_pcm_sha256": verified_pcm,
              "source_current_and_before_after_match": True,
              "diagnostic_health": {k: raw[k] for k in ("dropped_calls", "dropped_requests", "observation_error_count",
                  "pending_native_call_ids", "retained_pcm_bytes", "observation_hooks", "observation_total_ns", "observation_max_ns")},
              "load_request_seconds": (requests["stt-request-001"]["return"]["monotonic_ns"] - requests["stt-request-001"]["entry"]["monotonic_ns"]) / 1e9,
              "distinct_engine_identities": len({c["engine_identity"] for c in raw["calls"]}),
              "distinct_native_worker_threads": len({c["worker_entry"]["thread_id"] for c in raw["calls"]}),
              "slowest_recognition_call": slowest,
              "limits": [
                  "Passing audit checks mean artifact consistency, not four successful conversations. Baseline readiness failed before input and remains in the four-case denominator.",
                  "Call-to-turn mapping uses room, chronological order and exact transcript agreement; native records have no coordinator operation ID.",
                  "All seven submitted PCM hashes differ. Repeated source WAVs do not imply identical endpointed 16 kHz engine input.",
                  "Warmup is separate; initial model load has only its outer LocalSTT load request duration, not nested native stage attribution.",
                  "247 measured observer helper invocations total 3.143498 ms, maximum 0.546042 ms. These exclude some wrapper, clock and context propagation overhead and are not a bound on total observer impact.",
                  "Nested stage durations must not be added twice. Host-call wall time with unchanged lazy evaluation is not exclusive accelerator compute time.",
                  "The slowest observation is dominated by generation host wall time, not submission-to-worker delay. This does not explain any earlier uninstrumented 13-second observation or establish a root cause or performance improvement.",
                  "Model weights were not re-read during this audit; source files and retained PCM were hashed, and existing frozen receipts remain the model provenance evidence.",
              ]}
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(output), "counts": report["counts"], "slowest": slowest["call_id"]}))


if __name__ == "__main__":
    main()
