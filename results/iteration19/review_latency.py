"""Post-preflight analysis only; run after acoustic audit and manual speech review."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
MUTATIONS = {"start_navigation", "add_waypoint", "cancel_navigation"}


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
                if frame.get("rms_db", -999) >= threshold and handle["speech_id"] in
                (frame.get("heuristic_result_speech_id"), frame.get("heuristic_speech_id"))]
    if not matching:
        return {"first_monotonic": None, "reason": "No above-threshold frame tagged to this handle; untagged audio is not reassigned"}
    line, frame = matching[0]
    return {"first_monotonic": frame["received_monotonic"], "frame_line": line,
            "speech_id": handle["speech_id"],
            "report_first_loud_matches": handle.get("first_loud_at") == frame["received_monotonic"],
            "limit": "Recorder event-order attribution, not an audio watermark or physical playback time"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speech-review", type=Path, required=True)
    parser.add_argument("--audit", type=Path, default=BASE / "acoustic-audit.json")
    parser.add_argument("--protocol", type=Path, default=BASE / "protocol.json")
    parser.add_argument("--out", type=Path, default=BASE / "latency-review.json")
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    sources = [Path(__file__).resolve(), args.audit.resolve(), args.protocol.resolve(), args.speech_review.resolve()]
    hashes = {str(p.relative_to(ROOT)): sha(p) for p in sources}
    audit, protocol, speech = [json.loads(p.read_text()) for p in sources[1:]]
    declarations = {r["id"]: r for r in protocol["cases"]}
    pairs = [(arm["id"], case) for arm in protocol["arms"] for case in arm["cases"]]
    assert len(pairs) == len(set(pairs)) == protocol["selected_scope"]["captures"] == 10
    assert len(declarations) == len(protocol["cases"])
    assert all(declarations[case].get("recognition_delivery_delay_seconds", 0) == 0 for _, case in pairs)
    issues = []

    def index(rows, key, label):
        result = {}
        for row in rows:
            identity = key(row)
            if identity in result:
                raise ValueError(f"Duplicate {label} key: {identity}")
            result[identity] = row
        missing, extra = set(pairs) - result.keys(), result.keys() - set(pairs)
        if missing or extra:
            issues.append({"inventory": label, "missing": sorted(missing), "extra": sorted(extra)})
        return result

    for arm in audit["arms"]:
        run = arm.get("run_report") or {}
        if run.get("status") in {"starting", "running"} or run and not run.get("finished_at"):
            raise RuntimeError(f"Wait for terminal timed arm: {arm['arm_id']}")
    audit_rows = index([(arm["arm_id"], row) for arm in audit["arms"] for row in arm["cases"]],
                       lambda item: (item[0], item[1]["case_id"]), "acoustic audit")
    speech_rows = index(speech["cases"], lambda r: (r["arm"], r["case"]), "manual speech review")
    rows = []
    for arm_id, case_id in pairs:
        key, declaration = f"{arm_id}/{case_id}", declarations[case_id]
        case = audit_rows.get((arm_id, case_id), (arm_id, {}))[1]
        manual = speech_rows.get((arm_id, case_id), {})
        spoken = manual.get("final_required_facts_pass") is True
        effect_checks = case.get("effect_criteria", {})
        backend = bool(effect_checks) and all(c["status"] == "passed" for c in effect_checks.values())
        backend_agrees = manual.get("backend_expected_effect_sequence_pass") is backend
        if not backend_agrees:
            issues.append({"case": key, "issue": "Manual/backend gate mismatch or missing manual row",
                           "manual": manual.get("backend_expected_effect_sequence_pass"), "audit": backend})
        anchor_index = max(i for i, d in enumerate(declaration["inputs"]) if d.get("text") or d.get("segments"))
        inputs = case.get("inputs", [])
        anchor = inputs[anchor_index] if anchor_index < len(inputs) else {
            "declaration": declaration["inputs"][anchor_index], "capture": None, "times": {}}
        times, clip = anchor.get("times", {}), anchor.get("capture") or {}
        end = times.get("activity_end_monotonic")
        # Metadata for a prepared or partly published clip is not a received speech endpoint.
        published = clip.get("timing", {}).get("published_samples", 0)
        activity_end = clip.get("activity_end_sample")
        end_was_published = activity_end is not None and published >= activity_end
        if not end_was_published:
            end = None
        offset = (times["start_unix"] - times["start_monotonic"]
                  if times.get("start_unix") is not None and times.get("start_monotonic") is not None else None)
        end_unix = end + offset if end is not None and offset is not None else None
        handles = case.get("spoken_text_and_handles", {}).get("capture_handles", [])
        after = [h for h in handles if times.get("start_monotonic") is not None
                 and h.get("queued_at", -1) >= times["start_monotonic"]]
        version = max((h["intent_version"] for h in after), default=None)
        substantive = [h for h in after if h["intent_version"] == version and h["kind"] != "ack"]
        final = substantive[0] if len(substantive) == 1 else None
        acks = [h for h in after if h["kind"] == "ack"]
        first_ack = min(acks, key=lambda h: h["queued_at"]) if acks else None
        frames, frame_source = [], case.get("pcm", {}).get("raw_frame_source")
        if frame_source:
            path = ROOT / frame_source
            if path.is_file():
                hashes[frame_source] = sha(path)
                expected = audit.get("evidence_sha256", {}).get(frame_source)
                if expected != hashes[frame_source]:
                    issues.append({"case": key, "issue": "Frame source no longer matches audit hash", "path": frame_source})
                frames = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            else:
                issues.append({"case": key, "issue": "Missing frame source", "path": frame_source})
        report = case.get("capture_report", {})
        threshold = report.get("config", {}).get("threshold_db", -40)
        ack_pcm, final_pcm = tagged_pcm(first_ack, frames, threshold), tagged_pcm(final, frames, threshold)
        if final_pcm.get("report_first_loud_matches") is False:
            issues.append({"case": key, "issue": "Tagged substantive first PCM differs from report"})
        mutations = []
        for call in case.get("execution", {}).get("physical_calls", []):
            if call["function"] in MUTATIONS:
                mutations.append({"call": call, "relative_to_final_spoken_end_ms": delta(call.get("timestamp_start"), end_unix),
                                  "after_final_spoken_input_started": times.get("start_unix") is not None
                                  and call.get("timestamp_start") is not None and call["timestamp_start"] >= times["start_unix"]})
        relevant = [r for r in mutations if r["after_final_spoken_input_started"]]
        first_mutation = min(relevant, key=lambda r: r["call"]["timestamp_start"]) if relevant else None
        finished = bool(final and final["state"] == "finished" and final.get("error") is None)
        observed_finish = delta(final.get("ended_at"), end) if finished else None
        metrics = {"first_navigation_dispatch_intention": first_mutation["relative_to_final_spoken_end_ms"] if first_mutation else None,
                   "first_ack_queued": delta(first_ack["queued_at"], end) if first_ack else None,
                   "first_ack_pcm": delta(ack_pcm["first_monotonic"], end),
                   "final_substantive_first_pcm": delta(final_pcm["first_monotonic"], end),
                   "observed_final_reply_finished": observed_finish,
                   "fulfilled_final_reply_finished": observed_finish if backend and spoken and backend_agrees else None}
        categories = declaration["category"]
        group = next(g for g in ("positive_continuation", "cancellation_boundary", "query_boundary") if g in categories)
        rows.append({"key": key, "arm": arm_id, "case_id": case_id, "condition_group": group,
                     "audit_case_status": case.get("status", "missing"), "capture_status": report.get("status", "missing"),
                     "capture_completed": case.get("criteria", {}).get("capture_completed", {}).get("status", "unmeasured"),
                     "backend_case_passed": backend, "manual_final_speech_passed": spoken,
                     "backend_gate_agreement": backend_agrees, "failed_or_unmeasured_effect_criteria": {
                         name: c for name, c in effect_checks.items() if c["status"] != "passed"},
                     "input_anchor_index": anchor_index, "input_anchor": anchor,
                     "input_activity_end_was_published": end_was_published, "input_anchor_end_unix": end_unix,
                     "final_observed_intent_version": version, "final_handle_candidates": substantive,
                     "all_ack_handles_after_anchor_start": acks, "ack_pcm": ack_pcm, "final_substantive_pcm": final_pcm,
                     "all_navigation_attempts": mutations, "speech_end_to_observation_ms": metrics,
                     "missing_timing_reason": "Final declared speech endpoint not published/available" if end is None
                     else "No unique completed substantive reply" if not finished else None})
    groups = {}
    for arm_id in [a["id"] for a in protocol["arms"]] + ["all_arms"]:
        for group in ("all", "positive_continuation", "cancellation_boundary", "query_boundary", "cancellation_or_query_boundary"):
            selected = [r for r in rows if (arm_id == "all_arms" or r["arm"] == arm_id) and
                        (group == "all" or r["condition_group"] == group or group == "cancellation_or_query_boundary"
                         and r["condition_group"] in {"cancellation_boundary", "query_boundary"})]
            groups[f"{arm_id}/{group}"] = {metric: stats(selected, metric) for metric in rows[0]["speech_end_to_observation_ms"]}
    changed = [name for name, value in hashes.items() if sha(ROOT / name) != value]
    if changed:
        issues.append({"sources_changed_during_analysis": changed})
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "status": "completed" if not issues else "evidence_issue",
              "scope": "Analysis method written after runtime preflight while the baseline arm was running, without reading moving outputs; not part of evaluated frozen runtime. Executed only after completed acoustic audit and manual speech review.",
              "method": {"input_end": "Publication start plus WAV threshold activity_end_sample/rate, only when that sample was published; not a microphone/VAD endpoint.",
                         "anchor": "Last declared spoken input even when missing/unpublished; never replace a missing answer with the initial-command anchor.",
                         "dispatch": "Logger timestamp_start records dispatch intention before backend entry, mapped with this input's same-host unix/monotonic offset; not actual effect time.",
                         "substantive_reply": "Exactly one non-ack handle at latest observed intent queued after anchor start; one WAV may produce multiple finalized turns.",
                         "speech_gate": "Existing manual final facts plus all audited effect criteria and a finished error-free final handle; no automatic speech re-grading. Capture/trace/mechanism criteria stay separate.",
                         "percentiles": "Nearest rank sorted non-null milliseconds[ceil(n*p)-1]; all declared rows/nulls retained; p95 is the maximum at these small group sizes."},
              "per_case": rows, "groups": groups,
              "summary": {"declared_cases": len(rows), "backend_passed": sum(r["backend_case_passed"] for r in rows),
                          "speech_passed": sum(r["manual_final_speech_passed"] for r in rows),
                          "fulfilled_finish_measured": sum(r["speech_end_to_observation_ms"]["fulfilled_final_reply_finished"] is not None for r in rows)},
              "source_sha256": hashes, "evidence_issues": issues,
              "limits": ["Ten fixed-order development conversations; descriptive timings only, not a causal speed benefit, population statistic, or general completion guarantee.",
                         "No injected recognition hold is declared; no hold subtraction or inherited iteration18 probe grouping is used.",
                         "Ack queue is not ack PCM. Missing handle-specific audio tags stay null; untagged PCM is never reassigned to an acknowledgement.",
                         "Received PCM tags and speech-handle completion events are heuristic transport observations, not physical audibility or human intelligibility.",
                         "Observed completion of an incorrect/incomplete reply is retained while fulfilled completion is null. Missing captures remain declared rows.",
                         "Zero-effect cancellation/query boundaries can fulfill their request; a no-op cancellation attempt remains visible and is not called a navigation effect.",
                         "Backend/manual final facts do not imply initial clarification, continuation-hook exercise, trusted trace equality, or whole-scenario success."]}
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"status": result["status"], "summary": result["summary"], "issues": issues}, indent=2))
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
