#!/usr/bin/env python3
"""Recompute derived RTC reviews without inference or modifying captured artifacts.

Run from any directory. Only named derived JSON reviews are refreshed; existing
WAV samples must match. All captured artifacts and both pilots remain unchanged.
"""
from pathlib import Path
import hashlib
import json
import math
import wave

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
STACK = BASE / "local-stack-run1"
AIRPORT_TEXT = "Navigation to Kempegowda International Airport has started. The estimated time of arrival is 88 minutes."
OFFICE_TEXT = "The navigation has started to the Office (Manyata Tech Park). The previous destination, Kempegowda International Airport, has been replaced."
LIMITS = [
    "Ten serial synthetic trials: five baseline and five correction, including both pilots. Repeated identical inputs, warmed shared processes, fixed ordering and cache confounds; not held-out, human-driving or independent trial evidence.",
    "Five captures omit only the initial server agent_state=listening event before session_started. Exact whole-trace equality fails for those trials; all later server events match. Correct tool effects do not erase this capture-completeness limitation.",
    "Timing subtracts same-client monotonic observations from scheduled threshold speech end/onset. Input boundaries use 20ms frames at RMS >= -40dBFS, not human phoneme annotations. Capture lateness and queueing are separate from the schedule.",
    "tool_started/dispatch-log starts mark intention before backend entry; tool_done reports simulated backend success. Client receipt adds data delivery delay. Server wall timestamps are not subtracted from client monotonic values.",
    "Text/intent is matched to speech handles by adjacent event order; PCM has no utterance ID. One output track and nonoverlapping utterance windows support heuristic correlation, not exact text-to-RTP alignment or intelligibility.",
    "Old-result activity scans ALL threshold-exceeding frames before the next utterance queue, including frames the recorder stopped tagging after agent_state became listening. Last-frame arrival and following quiet-frame arrival are transport observations, not physical audible-stop bounds.",
    "interruption_requested is the coordinator's later request at finalized text, not confirmed playback cancellation or the time PCM ceased. Earlier cessation is consistent with SDK speech interruption; this capture does not directly identify its internal cause.",
    "Received speech is assessed separately in received-speech-review; no audio inference occurs in this audit. Declared grounded text and non-silent PCM alone do not establish complete, intelligible speech. Shared worker logs contain phonemizer word-count warnings during result synthesis.",
    "Exact concatenated WAVs preserve every received sample and existing silence, but insert no callback-arrival gaps. They are not reconstructed playout timelines. No microphone echo, speaker output, road noise or WAN behavior is measured.",
]


def load(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lines(path):
    return [(n, json.loads(line)) for n, line in enumerate(path.read_text().splitlines(), 1)]


def write_derived(path, value):
    content = json.dumps(value, indent=2) + "\n"
    if not path.exists() or path.read_text() != content:
        path.write_text(content)


def percentiles(values):
    ordered = sorted(v for v in values if v is not None)
    return {"n": len(ordered), **{f"p{p}_ms": round(ordered[math.ceil(len(ordered)*p/100)-1], 3)
            if ordered else None for p in (50, 95)}}


def audit(path, tool_rows):
    r = load(path / "report.json")
    events = lines(path / "events.jsonl")
    frames = lines(path / "frames.jsonl")
    ce = [(n, e) for n, e in events if e["kind"] == "coordinator_event"]
    server_path = STACK / "traces" / f"{r['room']}.jsonl"
    server = [e for _, e in lines(server_path)]
    received = [{k: e["event"][k] for k in ("type", "data", "ts")} for _, e in ce]
    missing = [{"reference": f"../local-stack-run1/traces/{r['room']}.jsonl:{n}", **e}
               for n, e in enumerate(server, 1) if e not in received]
    unexpected = [e for e in received if e not in server]
    calls = [{"reference": f"../local-stack-run1/tool-calls.jsonl:{n}", **e["call"]}
             for n, e in tool_rows if e["room"] == r["room"]]
    event_data = lambda typ: [(n, e) for n, e in ce if e["event"]["type"] == typ]
    starts, dones = event_data("tool_started"), event_data("tool_done")
    effects = [(n, e) for n, e in dones if e["event"]["data"]["tool"] in
               ("start_navigation", "add_waypoint", "cancel_navigation")]
    expected_destinations = ["P_AIRPORT"] + (["P_OFFICE"] if len(r["inputs"]) == 2 else [])
    observed_effects = [[e["event"]["data"]["tool"], e["event"]["data"]["result"]["destination_id"],
                         e["event"]["data"]["result"]["stop_ids"]] for _, e in effects]
    wm = load(path / "worker_manifest.json")
    checks = {
        "completed_observation": r["status"] == "completed_observation",
        "raw_artifact_hashes": all(sha(path / name) == h for name, h in r["artifact_sha256"].items()),
        "coordinator_trace_equality": received == server,
        "effect_sequence": observed_effects == [["start_navigation", p, []] for p in expected_destinations],
        "attempt_sequence": [c["function"] for c in calls] == ["search_destination", "compute_route", "start_navigation"] * len(expected_destinations),
        "all_attempts_done": all(c["status"] == "done" for c in calls),
        "attempt_event_coverage": len(calls) == len(starts) == len(dones) and all(any(
            c["attempt_id"] == e["event"]["data"]["execution_id"] + ":" + e["event"]["data"]["id"]
            and c["function"] == e["event"]["data"]["tool"] and c["args"] == e["event"]["data"]["args"]
            for _, e in starts) for c in calls),
        "no_stale_results": all(not e["event"]["data"]["stale"] for _, e in dones),
        "capture_worker_source_receipts_agree": all(wm["source_sha256"].get(str(ROOT / p), h) == h for p, h in r["source_sha256"].items()),
        "no_capture_errors": not r["receiver_errors"] and not r["cleanup_errors"],
    }
    error_events = [{"reference": f"events.jsonl:{n}", **e} for n, e in events
                    if "error" in e["kind"] or e.get("event", {}).get("type") in
                    ("tool_error", "tool_unknown", "tool_log_error", "tool_cancelled")]
    checks["no_error_events"] = not error_events
    turns = []
    for version, clip in enumerate(r["inputs"], 1):
        start = clip["timing"]["started_monotonic"]
        end = start + clip["activity_end_sample"] / clip["sample_rate"]
        final = next((n, e) for n, e in event_data("user_final") if e["event"]["data"]["intent_version"] == version)
        turn_starts = [(n, e) for n, e in starts if e["event"]["data"]["operation_id"] == f"turn-{version}"]
        effect = next((n, e) for n, e in effects if e["event"]["data"]["operation_id"] == f"turn-{version}")
        say = {e["event"]["data"]["kind"]: (n, e) for n, e in event_data("agent_say") if e["event"]["data"]["intent_version"] == version}
        metrics = {"transcript_ms": final, "ack_text_ms": say["ack"], "result_text_ms": say["result"],
                   "first_tool_notification_ms": turn_starts[0], "navigation_dispatch_notification_ms": turn_starts[-1], "navigation_success_notification_ms": effect}
        refs = {key: f"events.jsonl:{pair[0]}" for key, pair in metrics.items()}
        metrics = {key: round((pair[1]["observed_monotonic"] - end) * 1000, 3) for key, pair in metrics.items()}
        for kind in ("ack", "result"):
            handle = next(h for h in r["speech_handles"] if h["intent_version"] == version and h["kind"] == kind)
            first = next((n, f) for n, f in frames if handle["queued_at"] <= f["received_monotonic"] <= handle["ended_at"] and f["rms_db"] >= r["config"]["threshold_db"])
            metrics[f"first_{kind}_pcm_ms"] = round((first[1]["received_monotonic"] - end) * 1000, 3)
            refs[f"first_{kind}_pcm_ms"] = f"frames.jsonl:{first[0]}"
            if kind == "result":
                metrics["result_handle_finished_ms"] = round((handle["ended_at"]-end)*1000, 3) if handle["state"] == "finished" else None
                refs["result_handle_finished_ms"] = next((f"events.jsonl:{n}" for n, e in event_data("speech_handle")
                    if e["event"]["data"]["speech_id"] == handle["speech_id"] and e["event"]["data"]["state"] == "finished"), None)
        text = final[1]["event"]["data"]["text"]
        checks[f"transcript_{version}"] = text == ("Navigate to the airport." if version == 1 else "Actually, go to the office instead.")
        result = effect[1]["event"]["data"]["result"]
        declared = say["result"][1]["event"]["data"]["text"]
        checks[f"grounded_declared_result_{version}"] = declared == (AIRPORT_TEXT if version == 1 else OFFICE_TEXT) and result["destination_id"] == expected_destinations[version-1] and result["eta_min"] == (88 if version == 1 else 29) and (version == 1 or result["replaced"] == "Kempegowda International Airport")
        checks[f"result_after_effect_{version}"] = say["result"][1]["observed_monotonic"] > effect[1]["observed_monotonic"]
        lateness = [(e["capture_started_monotonic"] - e["scheduled_monotonic"]) * 1000 for _, e in events if e["kind"] == "input_frame_started" and e["label"] == clip["label"]]
        turns.append({"intent_version": version, "input": clip["label"], "speech_onset_monotonic": start + clip["activity_start_sample"] / clip["sample_rate"],
                      "speech_end_monotonic": end, "transcript": text, "declared_result": declared,
                      "metrics": metrics, "references": refs, "max_input_capture_lateness_ms": round(max(lateness), 3)})
    interruption = None
    if len(turns) == 2:
        old = next(h for h in r["speech_handles"] if h["kind"] == "result" and h["intent_version"] == 1)
        next_ack = next(h for h in r["speech_handles"] if h["kind"] == "ack" and h["intent_version"] == 2)
        # Do not rely on heuristic_result_speech_id: residual PCM can arrive
        # after agent_state changes to listening and hence loses that label.
        loud = [(n, f) for n, f in frames if old["queued_at"] <= f["received_monotonic"] < next_ack["queued_at"] and f["rms_db"] >= r["config"]["threshold_db"]]
        last_n, last = loud[-1]
        after = frames[last_n][1]
        onset = turns[1]["speech_onset_monotonic"]
        vad = next((n, e) for n, e in event_data("user_state") if e["observed_monotonic"] > onset and e["event"]["data"]["state"] == "speaking")
        listening = next((n, e) for n, e in event_data("agent_state") if e["observed_monotonic"] > onset and e["event"]["data"]["state"] == "listening")
        metrics = {"last_old_correlated_pcm_ms": last["received_monotonic"], "next_below_threshold_frame_ms": after["received_monotonic"],
                   "user_speaking_event_ms": vad[1]["observed_monotonic"], "agent_listening_event_ms": listening[1]["observed_monotonic"],
                   "coordinator_interruption_requested_ms": old["ended_at"]}
        interruption = {"origin": "correction input threshold speech onset", "metrics": {k: round((v-onset)*1000, 3) for k, v in metrics.items()},
                        "last_old_frame": f"frames.jsonl:{last_n}", "following_frame": f"frames.jsonl:{last_n+1}",
                        "last_old_pcm_after_user_speaking_ms": round((last["received_monotonic"]-vad[1]["observed_monotonic"])*1000, 3),
                        "untagged_tail_after_recorder_last_ms": round((last["received_monotonic"]-old["last_loud_at"])*1000, 3),
                        "old_handle_state": old["state"], "next_frame_rms_db": after["rms_db"],
                        "quiet_before_next_utterance_queue_ms": round((next_ack["queued_at"]-after["received_monotonic"])*1000, 3)}
        checks["correction_after_initial_effect"] = onset > effects[0][1]["observed_monotonic"]
        checks["old_result_interruption_requested"] = old["state"] == "interruption_requested"
    checks["speech_handle_states"] = all(h["state"] == ("interruption_requested" if len(turns) == 2 and h["kind"] == "result" and h["intent_version"] == 1 else "finished") and not h.get("error") for h in r["speech_handles"])
    assert len(r["tracks"]) == 1
    pcm = (path / r["tracks"][0]["file"]).read_bytes()
    offset = 0
    for _, f in frames:
        assert f["offset_bytes"] == offset and f["size_bytes"] == f["samples"] * 2
        offset += f["size_bytes"]
    assert offset == len(pcm)
    wav = path / "received-concatenated.wav"
    if not wav.exists():
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
    with wave.open(str(wav), "rb") as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1 and w.readframes(w.getnframes()) == pcm
    offsets = [e["received_unix"]-e["received_monotonic"] for _, e in events]
    return {"run": path.name, "room": r["room"], "pilot": path.name.endswith("run1"), "status": r["status"],
            "checks": checks, "all_checks_pass": all(checks.values()), "attempt_count": len(calls), "calls": calls,
            "observed_effects": observed_effects, "turns": turns, "interruption": interruption,
            "speech_handles": r["speech_handles"], "error_events": error_events,
            "client_wall_monotonic_offset_range_ms": round((max(offsets)-min(offsets))*1000, 3),
            "raw_sha256": {**r["artifact_sha256"], "report.json": sha(path / "report.json")},
            "server_trace_sha256": sha(server_path), "trace_comparison": {"server_event_count": len(server), "received_event_count": len(received),
                "missing_server_events": missing, "unexpected_received_events": unexpected,
                "received_is_exact_server_suffix": received == server[len(server)-len(received):]},
            "received_concatenated_wav": {"file": wav.name, "sha256": sha(wav), "duration_seconds": len(pcm)/48000,
                "label": "Exact PCM frame order with original silent samples; no inserted arrival gaps or physical playout claim."},
            "limitations": LIMITS}


def main():
    names = ["rtc-baseline-run1", "rtc-correction-run1"] + [r["name"] for r in load(BASE / "repeat-run.json")["runs"]]
    assert len(names) == len(set(names)) == 10
    tool_rows = lines(STACK / "tool-calls.jsonl")
    runs = [audit(BASE / name, tool_rows) for name in names]
    for run in runs:
        write_derived(BASE / run["run"] / "transport-review.json", run)
    groups = {"baseline_initial_including_cold_pilot": [r["turns"][0] for r in runs if "baseline" in r["run"]],
              "correction_initial_including_pilot": [r["turns"][0] for r in runs if "correction" in r["run"]],
              "correction_followup_including_pilot": [r["turns"][1] for r in runs if "correction" in r["run"]]}
    aggregate = {"kind": "Derived local RTC transport/effect review", "reproduce": "python3 results/iteration4/audit_transport.py",
                 "audit_script_sha256": sha(Path(__file__)), "run_order": names, "excluded_runs": [],
                 "counts": {"runs": len(runs), "all_checks_pass": sum(r["all_checks_pass"] for r in runs),
                     "backend_attempts": sum(r["attempt_count"] for r in runs), "navigation_effects": sum(len(r["observed_effects"]) for r in runs),
                     "transcribed_turns": sum(len(r["turns"]) for r in runs),
                     "expected_effect_sequence_pass": sum(r["checks"]["effect_sequence"] for r in runs),
                     "exact_trace_equality_pass": sum(r["checks"]["coordinator_trace_equality"] for r in runs),
                     "attempt_event_coverage_pass": sum(r["checks"]["attempt_event_coverage"] for r in runs),
                     "exact_transcript_pass": sum(v for r in runs for k,v in r["checks"].items() if k.startswith("transcript_")),
                     "grounded_declared_result_pass": sum(v for r in runs for k,v in r["checks"].items() if k.startswith("grounded_declared_result_"))},
                 "failed_checks": [{"run": r["run"], "checks": [k for k,v in r["checks"].items() if not v], "trace_comparison": r["trace_comparison"]}
                                   for r in runs if not r["all_checks_pass"]],
                 "percentile_method": "Nearest rank ceil(n*p/100)-1, including pilots; p95 is the maximum for n=5. No confidence or population-generalization claim.",
                 "speech_end_latencies_ms": {name: {metric: percentiles([t["metrics"][metric] for t in turns]) for metric in turns[0]["metrics"]} for name, turns in groups.items()},
                 "correction_onset_latencies_ms": {metric: percentiles([r["interruption"]["metrics"][metric] for r in runs if r["interruption"]]) for metric in next(r["interruption"]["metrics"] for r in runs if r["interruption"])},
                 "observations": [{"run": r["run"], "review": f"{r['run']}/transport-review.json", "checks_pass": r["all_checks_pass"], "turns": r["turns"], "interruption": r["interruption"]} for r in runs],
                 "limitations": LIMITS}
    write_derived(BASE / "aggregate-review.json", aggregate)
    print(json.dumps({"counts": aggregate["counts"], "aggregate": str(BASE / "aggregate-review.json")}, indent=2))


if __name__ == "__main__":
    main()
