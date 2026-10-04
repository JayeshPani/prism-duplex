"""Post-analysis only: case timing from retained captures, with independent speech gates."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def delta(end, start):
    return round((end - start) * 1000, 3) if end is not None and start is not None else None


def stats(rows, key):
    values = [r["speech_end_to_observation_ms"][key] for r in rows]
    valid = sorted(v for v in values if v is not None)
    return {"declared_cases": len(rows), "measured_cases": len(valid), "null_cases": len(rows) - len(valid),
            "case_keys_in_order": [r["key"] for r in rows], "values_ms_in_order": values,
            "p50_ms": valid[math.ceil(len(valid) * .5) - 1] if valid else None,
            "p95_ms": valid[math.ceil(len(valid) * .95) - 1] if valid else None}


def tagged_pcm(handle, frames, threshold):
    if not handle:
        return {"first_monotonic": None, "reason": "No unique matching speech handle"}
    matching = [(i, frame) for i, frame in enumerate(frames, 1)
                if frame.get("rms_db", -999) >= threshold
                and handle["speech_id"] in (frame.get("heuristic_result_speech_id"), frame.get("heuristic_speech_id"))]
    if not matching:
        return {"first_monotonic": None, "reason": "No above-threshold frame explicitly tagged to this handle; no assignment from untagged audio"}
    line, frame = matching[0]
    return {"first_monotonic": frame["received_monotonic"], "frame_line": line,
            "speech_id": handle["speech_id"], "report_first_loud_matches": handle.get("first_loud_at") == frame["received_monotonic"],
            "limit": "Recorder event-order attribution, not an audio watermark or physical playback time"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speech-review", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=BASE / "latency-review.json")
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    sources = [Path(__file__).resolve(), BASE / "acoustic-audit.json", BASE / "protocol.json", args.speech_review.resolve()]
    hashes = {str(p.relative_to(ROOT)): sha(p) for p in sources}
    audit = json.loads(sources[1].read_text())
    speech = json.loads(args.speech_review.read_text())
    # This adapter is intentionally tied to the saved independent review schema.
    speech_rows = {(r["arm"], r["case"]): r for r in speech["cases"]}
    assert len(speech_rows) == len(speech["cases"]) == 21
    rows, issues = [], []
    for arm in audit["arms"]:
        for case in arm["cases"]:
            arm_id, case_id = arm["arm_id"], case["case_id"]
            key = f"{arm_id}/{case_id}"
            manual = speech_rows[(arm_id, case_id)]
            spoken = manual["final_required_facts_pass"] is True
            backend = bool(case["effect_criteria"]) and all(c["status"] == "passed" for c in case["effect_criteria"].values())
            assert manual["backend_expected_effect_sequence_pass"] is backend, key
            inputs = case["inputs"]
            spoken_inputs = [(i, r) for i, r in enumerate(inputs) if r["declaration"].get("text") or r["declaration"].get("segments")]
            assert spoken_inputs, key
            anchor_index, anchor = spoken_inputs[-1]
            times = anchor["times"]
            end = times.get("activity_end_monotonic")
            offset = (times["start_unix"] - times["start_monotonic"]
                      if times.get("start_unix") is not None and times.get("start_monotonic") is not None else None)
            end_unix = end + offset if end is not None and offset is not None else None
            handles = case["spoken_text_and_handles"]["capture_handles"]
            after = [h for h in handles if times.get("start_monotonic") is not None and h["queued_at"] >= times["start_monotonic"]]
            version = max((h["intent_version"] for h in after), default=None)
            substantive = [h for h in after if h["intent_version"] == version and h["kind"] != "ack"]
            final = substantive[0] if len(substantive) == 1 else None
            acks = [h for h in after if h["kind"] == "ack"]
            first_ack = min(acks, key=lambda h: h["queued_at"]) if acks else None
            frames_path = ROOT / case["pcm"]["raw_frame_source"]
            hashes[str(frames_path.relative_to(ROOT))] = sha(frames_path)
            frames = [json.loads(line) for line in frames_path.read_text().splitlines() if line.strip()]
            threshold = case["capture_report"]["config"]["threshold_db"]
            ack_pcm = tagged_pcm(first_ack, frames, threshold)
            final_pcm = tagged_pcm(final, frames, threshold)
            if final_pcm.get("report_first_loud_matches") is False:
                issues.append({"case": key, "issue": "Tagged substantive first PCM differs from retained report"})
            mutations = []
            for call in case["execution"]["physical_calls"]:
                if call["function"] in {"start_navigation", "cancel_navigation", "add_waypoint"}:
                    mutations.append({"call": call, "relative_to_final_spoken_end_ms": delta(call.get("timestamp_start"), end_unix),
                                      "after_final_spoken_input_started": times.get("start_unix") is not None and call["timestamp_start"] >= times["start_unix"]})
            relevant = [r for r in mutations if r["after_final_spoken_input_started"]]
            first_mutation = min(relevant, key=lambda r: r["call"]["timestamp_start"]) if relevant else None
            finished = bool(final and final["state"] == "finished" and final.get("error") is None)
            observed_finish = delta(final.get("ended_at"), end) if finished else None
            metrics = {"first_navigation_dispatch_intention": first_mutation["relative_to_final_spoken_end_ms"] if first_mutation else None,
                       "first_ack_queued": delta(first_ack["queued_at"], end) if first_ack else None,
                       "first_ack_pcm": delta(ack_pcm["first_monotonic"], end),
                       "final_substantive_first_pcm": delta(final_pcm["first_monotonic"], end),
                       "observed_final_reply_finished": observed_finish,
                       "fulfilled_final_reply_finished": observed_finish if backend and spoken else None}
            delay = 5 if "delivery_delay5s" in case["declaration"].get("category", []) else 0
            group = "injected_delay_probes" if delay else "native_probes" if case_id.startswith("native_") else "native_language"
            delivery = []
            for call in case["delivery_diagnostics"]["calls"]:
                clock = lambda field: call.get(field, {}).get("monotonic_ns")
                duration = lambda right, left: round((clock(right) - clock(left)) / 1e6, 3) if clock(right) is not None and clock(left) is not None else None
                delivery.append({"call_id": call["call_id"], "status": call.get("status"), "requested_hold_seconds": call["delay_seconds"],
                                 "entry_to_original_engine_return_ms": duration("original_engine_return", "entry"),
                                 "observed_injected_hold_ms": duration("hold_end", "hold_start"),
                                 "entry_to_delivery_return_ms": duration("delivery_return", "entry"),
                                 "no_hold_reason": "Native condition has no injected hold" if call["delay_seconds"] == 0 else None})
            rows.append({"key": key, "arm": arm_id, "case_id": case_id, "condition_group": group, "declared_delivery_hold_seconds": delay,
                         "capture_status": case["capture_report"]["status"], "capture_completed": case["criteria"]["capture_completed"]["status"],
                         "backend_case_passed": backend, "manual_final_speech_passed": spoken,
                         "input_anchor_index": anchor_index, "input_anchor": anchor, "input_anchor_end_unix": end_unix,
                         "final_observed_intent_version": version, "final_handle_candidates": substantive,
                         "all_ack_handles_after_anchor_start": acks, "ack_pcm": ack_pcm, "final_substantive_pcm": final_pcm,
                         "all_navigation_attempts": mutations, "recognition_delivery_durations": delivery,
                         "speech_end_to_observation_ms": metrics,
                         "fulfillment_limit": "Backend and spoken facts gate completion only; this is not an interruption, trace-identity or whole-scenario pass",
                         "missing_timing_reason": "No published spoken input" if end is None else "No unique completed substantive reply" if not finished else None})
    assert len(rows) == 21
    groups = {}
    for arm_id in ("baseline", "candidate"):
        for group in ("all", "native_probes", "injected_delay_probes", "native_language"):
            selected = [r for r in rows if r["arm"] == arm_id and (group == "all" or r["condition_group"] == group)]
            if selected:
                groups[f"{arm_id}/{group}"] = {metric: stats(selected, metric) for metric in rows[0]["speech_end_to_observation_ms"]}
    changed = [name for name, value in hashes.items() if sha(ROOT / name) != value]
    if changed:
        issues.append({"sources_changed_during_analysis": changed})
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "status": "completed" if not issues else "evidence_issue",
              "scope": "Post-analysis method written after both timed arms and whole-stream ASR; not predeclared or a new experiment.",
              "method": {"input_end": "Scheduled publication start plus recorded WAV activity_end_sample/rate; threshold speech proxy, not VAD or microphone endpoint.",
                         "anchor": "Last declared spoken input; non-speech probes keep initial spoken-command anchor. All mutation attempts retained, including attempts before the final spoken input.",
                         "dispatch": "Physical call logger timestamp_start is dispatch intention before backend entry, not backend start or effect completion; mapped with this input's same-host unix/monotonic offset.",
                         "substantive_reply": "Require one non-ack handle at latest observed intent after anchor start; no assumption that one WAV equals one finalized turn.",
                         "speech_gate": "Use the independent saved speech review verbatim; all frozen effect criteria must pass. No automatic speech re-grading.",
                         "percentiles": "Nearest rank sorted valid values[ceil(n*p)-1], milliseconds; all declared null rows retained. p95 is effectively the maximum in these tiny groups."},
              "per_case": rows, "groups": groups, "summary": {"declared_cases": 21, "backend_passed": sum(r["backend_case_passed"] for r in rows),
                  "speech_passed": sum(r["manual_final_speech_passed"] for r in rows),
                  "fulfilled_finish_measured": sum(r["speech_end_to_observation_ms"]["fulfilled_final_reply_finished"] is not None for r in rows)},
              "source_sha256": hashes, "evidence_issues": issues,
              "limits": ["Descriptive small-n development timings, fixed arm order/shared host; no population accuracy, causal benefit or latency optimization claim.",
                         "No subtraction of the injected five-second hold: native engine and actual hold durations are displayed separately, and pause cases may contain multiple recognition calls.",
                         "Ack queue/finish are not ack PCM. Default recorder has no ack-specific frame tags, so unavailable ack PCM remains null.",
                         "PCM tags are heuristic client receipts; finished means received SpeechHandle completion event, not physical audibility or intelligibility.",
                         "The failed baseline readiness case and the finished but unfulfilled candidate ambiguity reply stay in the full denominator.",
                         "Backend/spoken-fact completion remains separate from old-reply interruption correctness and trusted trace completeness."]}
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"status": result["status"], "summary": result["summary"], "issues": issues}, indent=2))
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
