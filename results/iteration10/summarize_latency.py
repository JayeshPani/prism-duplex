"""Summarize the frozen RTC audit; stdlib only, no inference or raw writes."""
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
STACK = BASE / "local-stack-run1"
METRICS = ("transcript_ms", "ack_text_ms", "result_text_ms", "first_tool_intention_ms",
           "mutation_intention_ms", "mutation_success_ms", "first_ack_pcm_ms",
           "first_result_pcm_ms", "result_handle_finished_ms")
PHASES = {"tool_success_to_result_text_ms": "result_text_ms",
          "tool_success_to_result_pcm_ms": "first_result_pcm_ms"}
INTERRUPTION = ("last_old_pcm_after_input_onset_ms", "user_speaking_after_input_onset_ms",
                "handle_interruption_after_input_onset_ms", "untagged_tail_after_recorder_last_ms")


def load(path):
    return json.loads(path.read_text())


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def seconds(later, earlier):
    return round((datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds(), 6)


def stats(values):
    valid = sorted(value for value in values if value is not None)
    return {"n": len(valid), "missing_or_intentionally_interrupted": len(values) - len(valid),
            "values_ms_in_run_order": values,
            "p50_ms": valid[ceil(len(valid) * .5) - 1] if valid else None,
            "p95_ms": valid[ceil(len(valid) * .95) - 1] if valid else None,
            "min_ms": min(valid) if valid else None, "max_ms": max(valid) if valid else None}


def main():
    output = BASE / "latency-summary.json"
    if output.exists():
        raise FileExistsError("preserve the existing latency summary")
    audit, suite, protocol = [load(BASE / name) for name in ("audit.json", "run-report.json", "protocol.json")]
    assert suite.get("finished_at") and suite["status"] not in {"starting", "running"}
    turns = []
    for trial in audit["trials"]:
        # Missing audit rows keep the protocol-declared turn denominator.
        for version in range(1, 3 if protocol["expected"][trial["case"]].get("followup_audio") else 2):
            turn = next((row for row in trial.get("turns", []) if row["intent_version"] == version), {})
            handle = next((row for row in trial.get("speech_handles", [])
                           if row.get("kind") == "result" and row.get("intent_version") == version), {})
            metrics = {key: turn.get("speech_end_to_client_observation_ms", {}).get(key) for key in METRICS}
            state = handle.get("state")
            row = {"trial": trial["name"], "case": trial["case"], "intent_version": version,
                   "input": "initial" if version == 1 else "waypoint_followup" if trial["case"] == "waypoint" else "correction", "result_handle_state": state,
                   "result_finish_missing_reason": None if metrics["result_handle_finished_ms"] is not None else
                       "intentionally interrupted by next request" if state == "interruption_requested" else "missing or unfinished",
                   "speech_end_to_client_observation_ms": metrics}
            for key, metric in PHASES.items():
                row[key] = round(metrics[metric] - metrics["mutation_success_ms"], 3) if (
                    metrics[metric] is not None and metrics["mutation_success_ms"] is not None) else None
            turns.append(row)
    groups = {
        "initial_requests_all_four_including_first_run": [r for r in turns if r["intent_version"] == 1],
        "waypoint_followup_two": [r for r in turns if r["case"] == "waypoint" and r["intent_version"] == 2],
        "correction_followup_one": [r for r in turns if r["case"] == "correction" and r["intent_version"] == 2],
        "all_three_followups": [r for r in turns if r["intent_version"] == 2],
        "all_seven_turns": turns,
    }
    assert protocol["run_order"] == ["waypoint", "baseline", "waypoint", "correction"]
    parsed, unparsed = [], []
    for line, text in enumerate((STACK / "worker.log").read_text().splitlines(), 1):
        try:
            parsed.append({"line": line, **json.loads(text)})
        except ValueError:
            unparsed.append({"line": line, "text": text})
    warnings = [r for r in parsed if r.get("level") in {"WARNING", "WARN"}]
    for row in warnings:
        row["category"] = ("watchdog" if row["message"].startswith("event loop blocked") else
                           "phonemizer" if row.get("name") == "phonemizer" else
                           "native_unknown_handle" if "unknown FFI handle" in row["message"] else "other")
    unparsed_warnings = [r for r in unparsed if "Warning:" in r["text"] or "WARNING" in r["text"]]
    drain = [r for r in parsed if r.get("message") == "draining worker"]
    worker_exit = next(r for r in suite["cleanup"] if r["role"] == "worker")
    cleanup_durations, disconnect_timings, missing_packets, event_read_errors = [], [], [], []
    for trial in audit["trials"]:
        logs = trial.get("room_cleanup_logs", [])
        cleanup_durations.append(seconds(logs[-1]["timestamp"], logs[0]["timestamp"]) if len(logs) == 2 else None)
        try:
            events = rows(BASE / trial["name"] / "events.jsonl")
        except (OSError, ValueError) as error:
            events = []
            event_read_errors.append({"trial": trial["name"], "error": repr(error)})
        disconnected = next((r for r in events if r["kind"] == "disconnected"), None)
        disconnect_timings.append({"trial": trial["name"], "disconnect_to_cleanup_start_seconds":
            round(datetime.fromisoformat(logs[0]["timestamp"]).timestamp() - disconnected["received_unix"], 6)
            if logs and disconnected else None})
        for absent in trial.get("trace", {}).get("missing", []):
            matches = []
            for line, event in enumerate(events, 1):
                if event["kind"] != "data_packet":
                    continue
                try:
                    payload = json.loads(base64.b64decode(event["payload_base64"]))
                    if {k: payload[k] for k in ("type", "data", "ts")} == absent["event"]:
                        matches.append({"line": line, "participant": event["participant"], "topic": event["topic"]})
                except (ValueError, KeyError):
                    continue
            missing_packets.append({"trial": trial["name"], "missing_server_line": absent["line"], "raw_packet_matches": matches})
    memory = rows(BASE / "memory.jsonl")
    peaks, memory_errors = defaultdict(int), []
    for line, sample in enumerate(memory, 1):
        for role, processes in sample.get("owned_processes", {}).items():
            if isinstance(processes, dict):
                memory_errors.append({"line": line, "at_utc": sample["at_utc"], "role": role, **processes})
            else:
                peaks[role] = max(peaks[role], sum(p.get("rss", 0) for p in processes))
                for process in processes:
                    if process.get("error"):
                        memory_errors.append({"line": line, "at_utc": sample["at_utc"], "role": role, **process})
    evidence = [Path(__file__), BASE / "audit_live.py", BASE / "audit.json", BASE / "run-report.json",
                BASE / "protocol.json", STACK / "worker.log", BASE / "memory.jsonl", BASE / "pre-asr-cleanup.json",
                ROOT / "results/iteration8/latency-summary.json", ROOT / "agent/pipeline/local_stt.py"]
    summary = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "audit_sha256": digest(BASE / "audit.json"),
        "derivation_evidence_sha256": {str(p.relative_to(ROOT)): digest(p) for p in evidence},
        "method": "Same iteration8 nearest-rank method: sorted valid values[ceil(n*p)-1]. All declared turns retained; per-metric n and null count explicit. Values copy raw-audit metrics rounded to 0.001 ms. Phase intervals subtract mutation_success_ms from result_text_ms or first_result_pcm_ms and round to 0.001 ms.",
        "latency_origin": "Client monotonic receipt minus scheduled input speech end at the declared 20 ms RMS threshold. Tool intention receipt does not establish backend entry. Tool-success intervals use receipt of the declared successful state-changing outcome, not server service time.",
        "declared": {"captures": len(audit["trials"]), "initial_turns": 4, "waypoint_followups": 2, "correction_followups": 1, "all_turns": 7},
        "per_turn": turns,
        "speech_end_groups": {name: {key: stats([r["speech_end_to_client_observation_ms"][key] for r in group]) for key in METRICS} for name, group in groups.items()},
        "tool_success_phase_groups": {name: {key: stats([r[key] for r in group]) for key in PHASES} for name, group in groups.items()},
        "followup_input_onset_groups": {key: stats([(t.get("interruption") or {}).get(key) for t in audit["trials"] if protocol["expected"][t["case"]].get("followup_audio")]) for key in INTERRUPTION},
        "completion_census": dict(Counter(r["result_handle_state"] or "missing" for r in turns)),
        "trace": {"full_equality": audit["summary"]["checks"]["full_trace_equality"],
            "missing_classification_counts": dict(Counter(r["classification"] for t in audit["trials"] for r in t.get("trace", {}).get("missing", []))),
            "failures": [{"name": t["name"], "trace": t.get("trace"), "audit_error": t.get("audit_error")} for t in audit["trials"] if not t["checks"].get("full_trace_equality")],
            "undeclared_runs": audit["undeclared_runs"], "raw_packet_matches_for_missing_events": missing_packets, "event_read_errors": event_read_errors,
            "raw_packet_note": "Matching raw packets do not change the failed coordinator-event equality checks. The driver requires an identified agent participant before promoting a packet to a coordinator_event."},
        "execution": {"expected_attempts": sum(protocol["expected"][case]["attempts"] for case in protocol["run_order"]),
            "expected_effects": sum(len(protocol["expected"][case]["effects"]) for case in protocol["run_order"]),
            "observed_attempts": audit["summary"]["shared_attempts"], "attempt_status_counts": audit["summary"]["attempt_status_counts"],
            "observed_effects": audit["summary"]["observed_effects"], "attempts_without_end": audit["summary"]["attempts_without_end"],
            "unassigned_attempts": audit["unassigned_attempts"],
            "all_expected_effect_checks_passed": all(t["checks"].get("expected_effects") for t in audit["trials"]),
            "all_no_stale_checks_passed": all(t["checks"].get("no_stale_results") for t in audit["trials"]),
            "server_error_events": [r for t in audit["trials"] for r in t.get("server_error_events", [])],
            "capture_errors": {t["name"]: t.get("capture_errors") for t in audit["trials"]}},
        "warnings": {"total": len(warnings), "total_including_unparsed_warnings": len(warnings) + len(unparsed_warnings),
            "counts": dict(Counter(r["category"] for r in warnings)), "all_rows": warnings,
            "error_rows": [r for r in parsed if r.get("level") in {"ERROR", "CRITICAL"}],
            "unparsed_worker_lines": unparsed, "unparsed_startup_warnings": unparsed_warnings,
            "limit": "All warnings retained. Native handle IDs do not identify an object or prove audio loss or a leak. Watchdog and STT logs are retained without attributing their causes. The short HMAC-key warning concerns intentional local development credentials."},
        "cleanup": {"room_cleanup_started": sum(any(r.get("message", "").startswith("room cleanup started:") for r in t.get("room_cleanup_logs", [])) for t in audit["trials"]),
            "room_cleanup_finished": sum(any(r.get("message", "").startswith("room cleanup finished:") for r in t.get("room_cleanup_logs", [])) for t in audit["trials"]),
            "room_cleanup_seconds_in_run_order": cleanup_durations, "disconnect_to_cleanup_start": disconnect_timings,
            "agent_departures": sum(t["checks"].get("agent_departed", False) for t in audit["trials"]),
            "agent_departure_observation_seconds_in_run_order": [(t.get("room_departure") or {}).get("elapsed_seconds") for t in audit["trials"]],
            "worker_drain_log_to_exit_seconds": seconds(worker_exit["finished_at"], drain[-1]["timestamp"]) if drain else None,
            "drain_logs": drain, "process_receipts": suite["cleanup"], "pre_asr_pid_verification": load(BASE / "pre-asr-cleanup.json")},
        "memory": {"scope": suite["memory_scope"], "samples": len(memory), "started_at": memory[0]["at_utc"], "finished_at": memory[-1]["at_utc"],
            "minimum_system_available_bytes": min(r["system_memory"]["available"] for r in memory),
            "swap_used_start_bytes": memory[0]["swap"]["used"], "swap_used_end_bytes": memory[-1]["swap"]["used"],
            "swap_used_max_bytes": max(r["swap"]["used"] for r in memory), "peak_summed_rss_bytes_by_owned_role": dict(peaks),
            "sampling_errors": memory_errors, "limit": "RSS sums can share pages and omit accelerator allocation. System swap includes other applications; correlation does not identify the cause of the slow recognition interval. Sampling errors at shutdown remain visible and PID absence is checked separately."},
        "stt_worker_logs": [r for r in parsed if r.get("name") == "prism.stt"],
        "frozen_inputs_unchanged_at_end": audit["frozen_inputs_unchanged_at_end"],
        "current_frozen_input_mismatches": audit["current_frozen_input_mismatches"],
        "limits": ["Four repeated development captures, two waypoint followups and one destination correction, fixed order and shared models; p95 is the maximum for these small groups. No population or isolated causal comparison.",
            "Every declared turn remains in its denominator. Intentionally interrupted original results have null finish metrics; unfinished or missing final results are retained.",
            "Phase differences include responder processing and client scheduling/transport; PCM also includes TTS. Neither is physical audible latency or isolated model service time.",
            "Old PCM tail uses every above-threshold frame before the next queued utterance, including untagged frames. Association is heuristic; speech-handle interruption is not a measured acoustic stop.",
            "Received-content ASR is a separate artifact; these counts and timings do not establish intelligibility or answer completeness. No probes were declared.",
            "Worker duration begins at the drain log, not exact signal send. Cleanup logs, room absence, process exit and sampled PID absence are separate observations."],
    }
    with output.open("x") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"phases": summary["tool_success_phase_groups"]["all_seven_turns"], "warnings": summary["warnings"]["counts"], "memory": summary["memory"]}, indent=2))


if __name__ == "__main__":
    main()
