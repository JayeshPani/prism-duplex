"""Read terminal iteration13 artifacts; preserve failures and transport identities."""
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
STACK = BASE / "local-stack-run1"


def load(path):
    return json.loads(path.read_text())


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def first(items, predicate):
    return next((item for item in items if predicate(item)), None)


def parse_log(path):
    result = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            record = json.loads(line)
        except ValueError:
            fields = line.split("\t", 5)
            record = {"raw": line}
            if len(fields) == 6:
                try:
                    record = dict(timestamp=fields[0], level=fields[1], logger=fields[2],
                                  source=fields[3], message=fields[4], data=json.loads(fields[5]))
                except ValueError:
                    pass
        result.append({"line": number, **record})
    return result


def attribution(events, report):
    packets = [row for row in events if row["kind"] == "data_packet"]
    by_id = {row["packet_id"]: row for row in packets}
    dispositions = [row for row in events if row["kind"] == "data_attribution"]
    promoted = [row for row in events if row["kind"] == "coordinator_event"]
    histories = defaultdict(list)
    for row in dispositions:
        histories[row["packet_id"]].append(row)
    mismatches, delays, buffered = [], [], []
    for row in promoted:
        packet = by_id.get(row["packet_id"])
        identity = None if packet is None else packet.get("native_participant_identity")
        if packet is not None and identity is None:
            identity = packet.get("participant")
        if not packet or not identity or row.get("participant") != identity:
            mismatches.append({"packet_id": row["packet_id"], "reason": "transport identity differs"})
        if packet and any(row[key] != packet[key] for key in ("received_monotonic", "received_unix")):
            mismatches.append({"packet_id": row["packet_id"], "reason": "receipt timestamp differs"})
        if packet and row["observed_monotonic"] != packet["received_monotonic"]:
            mismatches.append({"packet_id": row["packet_id"], "reason": "observed timestamp differs"})
        delays.append((row["attribution_resolved_monotonic"] - row["observed_monotonic"]) * 1000)
    for packet_id, history in histories.items():
        if any(row["status"] == "buffered" for row in history):
            packet = by_id[packet_id]
            identity = packet.get("native_participant_identity")
            if identity is None:
                identity = packet.get("participant")
            buffered.append({"packet_id": packet_id, "transport_identity": identity,
                             "public_participant_at_receipt": packet.get("participant"),
                             "dispositions": history,
                             "participant_connected_rows": [row for row in events
                                 if row["kind"] == "participant_connected" and row["participant"] == identity],
                             "promoted_rows": [row for row in promoted if row["packet_id"] == packet_id]})
    counts = dict(Counter(row["status"] for row in dispositions))
    return {"packet_count": len(packets), "public_participant_missing": sum(row.get("participant") is None for row in packets),
            "native_identity_missing_or_blank": sum(not row.get("native_participant_identity", "") for row in packets),
            "public_native_identity_disagreement": [row["packet_id"] for row in packets
                if row.get("participant") and row.get("native_participant_identity") is not None
                and row["participant"] != row["native_participant_identity"]],
            "disposition_counts": counts, "reported_counts": report.get("data_attribution_counts"),
            "counts_match_report": counts == report.get("data_attribution_counts"),
            "buffered_packets": buffered, "promoted_count": len(promoted),
            "duplicate_promotions": [key for key, count in Counter(row["packet_id"] for row in promoted).items() if count > 1],
            "transport_identity_or_receipt_mismatches": mismatches,
            "promotion_delay_ms": {"minimum": min(delays) if delays else None, "maximum": max(delays) if delays else None},
            "sdk_adapter": report.get("rtc_identity_adapter")}


def main():
    output = BASE / "supplementary-log-review.json"
    if output.exists():
        raise FileExistsError(output)
    suite, audit, protocol = (load(BASE / name) for name in ("run-report.json", "audit.json", "protocol.json"))
    if not suite.get("finished_at") or suite["status"] in {"starting", "running"}:
        raise RuntimeError("Wait for terminal suite and cleanup")
    paths = [STACK / "worker.log", STACK / "llm.log", STACK / "livekit.log"]
    paths += [BASE / (row["name"] + ".log") for row in suite["runs"]]
    parsed = {path: parse_log(path) for path in paths}
    worker = parsed[STACK / "worker.log"]
    warnings = {str(path.relative_to(ROOT)): [row for row in content
        if row.get("level") in {"WARN", "WARNING"} or re.search(r"\b\w*Warning:", row.get("raw", ""))]
        for path, content in parsed.items()}
    errors = {str(path.relative_to(ROOT)): [row for row in content
        if row.get("level") in {"ERROR", "CRITICAL", "FATAL"} or row.get("raw", "").startswith("Traceback (most recent call last)")]
        for path, content in parsed.items()}
    aec = load(STACK / "aec-discard-diagnostics.json")
    captures, cleanup, gates, traces = [], [], [], []
    evidence = set(paths + [Path(__file__), BASE / "run-report.json", BASE / "audit.json", BASE / "audit_live.py",
                           BASE / "protocol.json", BASE / "pre-asr-cleanup.json", STACK / "aec-discard-diagnostics.json",
                           STACK / "tool-calls.jsonl", ROOT / "scripts/local_livekit_audio_experiment.py"])
    for run in suite["runs"]:
        directory = BASE / run["name"]
        report, events, frames = load(directory / "report.json"), rows(directory / "events.jsonl"), rows(directory / "frames.jsonl")
        room = report["room"]
        trace_path = STACK / "traces" / (room + ".jsonl")
        trace = rows(trace_path)
        evidence.update([directory / name for name in ("report.json", "events.jsonl", "frames.jsonl")])
        evidence.add(trace_path)
        decoded = []
        for row in events:
            if row["kind"] == "data_packet" and row.get("topic") == "agent-events":
                try:
                    payload = json.loads(base64.b64decode(row["payload_base64"]))
                    decoded.append({key: payload[key] for key in ("type", "data", "ts")})
                except (ValueError, KeyError):
                    pass
        received = [{key: row["event"][key] for key in ("type", "data", "ts")}
                    for row in events if row["kind"] == "coordinator_event"]
        timings = [clip for clip in report["inputs"] if "timing" in clip]
        captures.append({"trial": run["name"], "room": room, "status": report["status"],
                         "failed_stage": report.get("failed_stage"), "error": report.get("error"),
                         "capture_traceback": report.get("traceback"), "prepared_inputs": len(report["inputs"]),
                         "published_inputs": len(timings), "received_frames": len(frames),
                         "frames_above_activity_threshold": sum(frame["rms_db"] >= report["config"]["threshold_db"] for frame in frames),
                         "full_promoted_trace_equality": received == trace, "raw_decoded_trace_equality": decoded == trace,
                         "attribution": attribution(events, report),
                         "input_drain_minus_scheduled_end_ms": [{"label": clip["label"], "ms": round((clip["timing"]["drained_monotonic"] - clip["timing"]["scheduled_end_monotonic"]) * 1000, 3)} for clip in timings]})
        callbacks = [row for row in worker if row.get("message") in {f"room cleanup started: {room}", f"room cleanup finished: {room}"}]
        trace_receipts = []
        for row in worker:
            match = re.search(r"trace cleanup: .*" + re.escape(room) + r"\.jsonl complete=(\w+) accepted=(\d+) flushed=(\d+) rejected=(\d+) pending=(\w+)", row.get("message", ""))
            if match:
                trace_receipts.append({"log": row, "complete": match[1] == "True", "accepted": int(match[2]),
                    "flushed": int(match[3]), "rejected": int(match[4]), "pending": match[5] == "True",
                    "actual_trace_lines": len(trace), "counts_equal_trace_lines": int(match[2]) == int(match[3]) == len(trace)})
        traces.extend(trace_receipts)
        disconnected = first(events, lambda row: row["kind"] == "disconnected")
        cleanup.append({"trial": run["name"], "departure": run.get("room_departure"), "callbacks": callbacks,
            "cleanup_callback_ms": round((datetime.fromisoformat(callbacks[-1]["timestamp"]) - datetime.fromisoformat(callbacks[0]["timestamp"])).total_seconds() * 1000, 3) if len(callbacks) == 2 else None,
            "client_disconnected_callback": disconnected,
            "cleanup_start_after_client_disconnect_ms": round((datetime.fromisoformat(callbacks[0]["timestamp"]).timestamp() - disconnected["received_unix"]) * 1000, 3) if callbacks and disconnected else None,
            "trace_writer": trace_receipts, "receiver_errors": report.get("receiver_errors", []), "cleanup_errors": report.get("cleanup_errors", [])})
        follow = first(timings, lambda clip: clip["label"] == "correction")
        bounds = [follow["timing"]["started_monotonic"] + follow[key] / follow["sample_rate"] for key in ("activity_start_sample", "activity_end_sample")] if follow else None
        ack = first(report["speech_handles"], lambda row: row.get("intent_version") == 1 and row.get("kind") == "ack")
        intervals = []
        for obj in aec["recognition_objects"]:
            if obj["room"] != room:
                continue
            for original in obj["intervals"]:
                item = dict(original)
                end = item.get("next_unsubstituted_monotonic", item["last_monotonic"])
                item.update(source_audio_seconds=sum(value / int(key.split("Hz")[0]) for key, value in item["samples_by_format"].items()),
                    overlaps_scheduled_followup_activity=bool(bounds and item["first_monotonic"] < bounds[1] and end > bounds[0]),
                    following_input_onset_minus_next_unsubstituted_ms=round((bounds[0] - end) * 1000, 3) if bounds else None,
                    within_initial_ack_handle_interval=bool(ack and ack["queued_at"] <= item["first_monotonic"] <= item["last_monotonic"] <= ack["ended_at"]))
                intervals.append(item)
        gates.append({"trial": run["name"], "room": room, "counts": aec["room_totals"].get(room),
                      "substituted_intervals": intervals, "followup_scheduled_activity_monotonic": bounds})
    memory = rows(BASE / "memory.jsonl")
    evidence.add(BASE / "memory.jsonl")
    memory_errors = []
    for number, row in enumerate(memory, 1):
        for role, values in row.get("owned_processes", {}).items():
            for value in values if isinstance(values, list) else [values]:
                if "error" in value:
                    memory_errors.append({"line": number, "at_utc": row["at_utc"], "role": role, **value})
    attribution_counts = Counter()
    for capture in captures:
        attribution_counts.update(capture["attribution"]["disposition_counts"])
    jobs = [row for row in parsed[STACK / "livekit.log"] if row.get("message") == "job ended"]
    intervals = [item for gate in gates for item in gate["substituted_intervals"]]
    buffered = sum(len(capture["attribution"]["buffered_packets"]) for capture in captures)
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "All declared iteration13 captures and frozen artifacts; transport identity, deferred attribution, warnings, memory and cleanup. No model/service launches or raw changes.",
        "line_convention": "One-based lines in referenced artifacts.",
        "summary": {"captures_completed": sum(row["status"] == "completed_observation" for row in captures), "declared_captures": len(protocol["run_order"]),
            "expected_attempts": sum(protocol["expected"][case]["attempts"] for case in protocol["run_order"]), "done_attempts": audit["summary"]["successful_attempts"],
            "expected_effects": sum(len(protocol["expected"][case]["effects"]) for case in protocol["run_order"]), "observed_effects": audit["summary"]["observed_effects"],
            "full_promoted_trace_equality": sum(row["full_promoted_trace_equality"] for row in captures), "raw_decoded_trace_equality": sum(row["raw_decoded_trace_equality"] for row in captures),
            "attribution_counts": dict(attribution_counts), "buffered_packets": buffered,
            "delayed_attribution_exercised": buffered > 0,
            "trace_writer": {"accepted": sum(row["accepted"] for row in traces), "flushed": sum(row["flushed"] for row in traces), "rejected": sum(row["rejected"] for row in traces), "complete_rooms": sum(row["complete"] for row in traces), "counts_match_raw_traces": all(row["counts_equal_trace_lines"] for row in traces)},
            "warning_rows": sum(map(len, warnings.values())), "warning_counts_by_log": {key: len(value) for key, value in warnings.items()},
            "runtime_error_level_or_traceback_count": sum(map(len, errors.values())),
            "server_job_statuses": dict(Counter(row["data"]["status"] for row in jobs))},
        "capture_census": captures, "failed_checks": audit["failed_checks"], "audit_check_denominators": audit["summary"]["checks"],
        "attribution_interpretation": "Buffered packets and their eventual dispositions determine live delayed-attribution coverage. Identity comes only from native transport metadata or the public participant; payload contents never establish sender identity. Raw packet decoding is a separate posthoc equality check, not authorization.",
        "warnings": warnings, "errors": errors,
        "watchdogs": [row for row in worker if row.get("message", "").startswith("event loop blocked")],
        "stt_worker_logs": [row for row in worker if row.get("name") == "prism.stt"], "server_job_rows": jobs,
        "gate_alignment": gates, "aec_diagnostics": {"status": aec["status"], "source_hashes_unchanged": aec["source_hashes_unchanged"],
            "observed_frames": sum(obj["frames"] for obj in aec["recognition_objects"]), "substituted_frames": sum(obj["substituted_frames"] for obj in aec["recognition_objects"]),
            "substituted_audio_seconds": sum(item["source_audio_seconds"] for item in intervals), "followup_overlap_count": sum(item["overlaps_scheduled_followup_activity"] for item in intervals),
            "all_substituted_frames_aec_active": all(item["aec_active_frames"] == item["frames"] for item in intervals),
            "observation_errors": aec["observation_errors"], "identity_errors": [obj["identity_error"] for obj in aec["recognition_objects"] if obj["identity_error"]],
            "observation_total_ns": aec["observation_total_ns"], "observation_max_ns": aec["observation_max_ns"]},
        "cleanup": {"rooms": cleanup, "services": suite["cleanup"], "process_absence_receipt": load(BASE / "pre-asr-cleanup.json"),
                    "drain_logs": [row for row in worker if row.get("message") == "draining worker"]},
        "memory": {"scope": suite["memory_scope"], "samples": len(memory), "min_system_available_bytes": min(row["system_memory"]["available"] for row in memory),
            "swap_used_start_bytes": memory[0]["swap"]["used"], "swap_used_end_bytes": memory[-1]["swap"]["used"], "swap_used_peak_bytes": max(row["swap"]["used"] for row in memory), "sampling_errors": memory_errors},
        "evidence_sha256": {str(path.relative_to(ROOT)): digest(path) for path in sorted(evidence)},
        "limits": ["Four serial development cases, shared warm stack and instrumentation; no general accuracy or causal historical timing comparison.",
            "Scheduled input and client frame arrival are transport observations, not physical audible latency. Input pacing errors remain in original receipts.",
            "Public participant absence differs from native sender identity absence; no sender is inferred from event payload.",
            "Server JS_FAILED, application cleanup receipts and worker exit0 remain distinct; none alone certifies all application work.",
            "Memory/swap correlation does not identify a cause of latency. Watchdog stacks only establish the sampled location.",
            "AEC counters describe observed substitutions, not per-sample sender attribution or proof that meaningful speech was lost."]}
    with output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps(result["summary"], indent=2))
    print("sha256", digest(output))


if __name__ == "__main__":
    main()
