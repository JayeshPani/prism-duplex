"""Derive the declared RTC audit after suite termination; no inference or raw writes."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT, STACK = BASE.parent.parent, BASE / "local-stack-run1"
ERROR_TYPES = {"tool_error", "tool_unknown", "tool_log_error", "tool_cancelled"}


def load(path):
    return json.loads(path.read_text())


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def mismatch(base, hashes):
    return [name for name, expected in hashes.items()
            if not (base / name).is_file() or digest(base / name) != expected]


def first(items, predicate):
    return next((item for item in items if predicate(item)), None)


def difference(left, right):
    """Multiset subtraction preserves duplicate occurrences and source order."""
    available = Counter(json.dumps(item, sort_keys=True) for item in right)
    missing = []
    for index, item in enumerate(left, 1):
        key = json.dumps(item, sort_keys=True)
        if available[key]:
            available[key] -= 1
        else:
            missing.append({"line": index, "event": item})
    return missing


def is_effect(row):
    result = row["data"]["result"]
    if result.get("status") != "success":
        return False
    tool = row["data"]["tool"]
    if tool == "start_navigation":
        return result.get("already_active") is False
    if tool == "add_waypoint":
        return result.get("already_present") is False
    return tool == "cancel_navigation" and bool(result.get("cancelled"))


def audit_trial(entry, case, expected, shared, worker_logs, frozen):
    directory = BASE / entry["name"]
    report = load(directory / "report.json")
    events, frames = rows(directory / "events.jsonl"), rows(directory / "frames.jsonl")
    trace_path = STACK / "traces" / (report["room"] + ".jsonl")
    server = rows(trace_path)
    client = [row for row in events if row["kind"] == "coordinator_event"]
    received = [{key: row["event"][key] for key in ("type", "data", "ts")} for row in client]
    missing, extra = difference(server, received), difference(received, server)
    disconnect = first(events, lambda row: row["kind"] == "disconnected")
    session = first(server, lambda row: row["type"] == "session_started")
    for row in missing:
        event = row["event"]
        row["classification"] = ("after_client_disconnect" if disconnect and event["ts"] >= disconnect["received_unix"]
            else "initial_listening" if row["line"] == 1 and event["type"] == "agent_state"
            and event["data"] == {"state": "listening"} and session and event["ts"] < session["ts"]
            else "other_missing")
    calls = [row["call"] for row in shared if row["room"] == report["room"]]
    started = [row for row in server if row["type"] == "tool_started"]
    done = [row for row in server if row["type"] == "tool_done"]
    identity = lambda row: row["data"]["execution_id"] + ":" + row["data"]["id"]
    paired = len(calls) == len(started) == len(done) and len({c.get("attempt_id") for c in calls}) == len(calls)
    paired = paired and all(sum(identity(row) == call.get("attempt_id") and row["data"]["tool"] == call["function"]
                               and row["data"]["args"] == call["args"] for row in collection) == 1
                               for call in calls for collection in (started, done))
    writes = [row for row in done if row["data"]["tool"] in {"start_navigation", "add_waypoint", "cancel_navigation"}]
    effects = [row for row in writes if is_effect(row)]
    sequence = [{"tool": row["data"]["tool"], "destination_id": row["data"]["result"].get("destination_id"),
                 "stop_ids": row["data"]["result"].get("stop_ids")} for row in effects]
    expected_sequence = [{key: row[key] for key in ("tool", "destination_id", "stop_ids")} for row in expected["effects"]]
    offset, frame_errors = defaultdict(int), []
    for index, frame in enumerate(frames, 1):
        name = frame["pcm_file"]
        if frame["offset_bytes"] != offset[name] or frame["size_bytes"] != frame["samples"] * 2:
            frame_errors.append(index)
        offset[name] = frame["offset_bytes"] + frame["size_bytes"]
    artifact_bad = mismatch(directory, report.get("artifact_sha256", {}))
    capture_frozen_bad = [name for name, value in report["source_sha256"].items() if frozen.get(str(ROOT / name)) != value]
    worker_receipt = load(directory / "worker_manifest.json")
    worker_bad = [name for name, value in report["source_sha256"].items() if name.startswith("agent/")
                  and worker_receipt["source_sha256"].get(str(ROOT / name)) != value]
    cleanup_logs = [row for row in worker_logs if row.get("message") in
                    {f"room cleanup started: {report['room']}", f"room cleanup finished: {report['room']}"}]
    checks = {
        "capture_completed": entry.get("exit_code") == 0 and report["status"] == ("completed_probe_observation" if case in {"silence", "noise"} else "completed_observation"),
        "raw_hashes": bool(report.get("artifact_sha256")) and not artifact_bad,
        "frozen_source_receipts": not capture_frozen_bad and not worker_bad,
        "full_trace_equality": received == server,
        "attempt_sequence": [row["function"] for row in calls] == expected["attempt_sequence"],
        "attempt_count": len(calls) == expected["attempts"], "attempt_event_pairs": paired,
        "all_attempts_done": bool(calls) and all(row.get("status") == "done" and "timestamp_end" in row for row in calls),
        "all_done_results_successful": bool(done) and all(row["data"]["result"].get("status") == "success" for row in done),
        "expected_effects": sequence == expected_sequence and len(writes) == len(effects),
        "declared_effect_facts": len(effects) == len(expected["effects"]) and all(
            all(actual["data"]["result"].get(key) == value for key, value in declared.items() if key != "tool")
            for actual, declared in zip(effects, expected["effects"])),
        "no_stale_results": all(not row["data"].get("stale") for row in done),
        "frame_integrity": bool(frames) and len(report["tracks"]) == 1 and not frame_errors and all((directory / name).stat().st_size == size for name, size in offset.items()),
        "no_capture_errors": not report.get("receiver_errors") and not report.get("cleanup_errors"),
        "no_server_tool_errors": not any(row["type"] in ERROR_TYPES for row in server),
        "agent_departed": bool(entry.get("room_departure", {}).get("agent_departed")),
        "room_cleanup_logged": [row["message"] for row in cleanup_logs] == [f"room cleanup started: {report['room']}", f"room cleanup finished: {report['room']}"],
    }
    turns = []
    clips = [clip for clip in report["inputs"] if clip["label"] in {"initial", "correction"}]
    checks["declared_input_count"] = len(clips) == (2 if expected.get("followup_audio") else 1)
    declared_audio = [expected["initial_audio"]] + ([expected["followup_audio"]] if expected.get("followup_audio") else [])
    checks["declared_input_audio"] = len(clips) == len(declared_audio) and all(
        clip["sha256"] == digest(BASE / "audio-assets" / name) for clip, name in zip(clips, declared_audio))
    for version, clip in enumerate(clips, 1):
        start = clip.get("timing", {}).get("started_monotonic")
        end = start + clip["activity_end_sample"] / clip["sample_rate"] if start is not None else None
        checks[f"input_published_{version}"] = start is not None
        turn_events = [row for row in client if row["event"]["data"].get("intent_version") == version]
        utterances = [row["event"]["data"] for row in turn_events if row["event"]["type"] == "agent_say"]
        notification = {"transcript": first(turn_events, lambda row: row["event"]["type"] == "user_final")}
        for kind in ("ack", "result"):
            notification[kind + "_text"] = first(turn_events, lambda row: row["event"]["type"] == "agent_say" and row["event"]["data"].get("kind") == kind)
        operation = [row for row in client if row["event"]["data"].get("operation_id") == f"turn-{version}"]
        notification["first_tool_intention"] = first(operation, lambda row: row["event"]["type"] == "tool_started")
        mutation = expected["effects"][version - 1]["tool"] if version <= len(expected["effects"]) else None
        for event_type, key in (("tool_started", "mutation_intention"), ("tool_done", "mutation_success")):
            notification[key] = first(operation, lambda row: row["event"]["type"] == event_type
                and row["event"]["data"].get("tool") == mutation
                and (event_type != "tool_done" or is_effect(row["event"])))
        metrics = {key + "_ms": round((row["observed_monotonic"] - end) * 1000, 3) if row and end is not None else None for key, row in notification.items()}
        for kind in ("ack", "result"):
            handle = first(report["speech_handles"], lambda row: row.get("intent_version") == version and row.get("kind") == kind)
            pcm = first(frames, lambda row: handle and handle["queued_at"] <= row["received_monotonic"] <= handle.get("ended_at", float("inf")) and row["rms_db"] >= report["config"]["threshold_db"])
            metrics[f"first_{kind}_pcm_ms"] = round((pcm["received_monotonic"] - end) * 1000, 3) if pcm and end is not None else None
            if kind == "result":
                metrics["result_handle_finished_ms"] = round((handle["ended_at"] - end) * 1000, 3) if handle and handle["state"] == "finished" and end is not None else None
        transcript = notification["transcript"]["event"]["data"]["text"] if notification["transcript"] else None
        checks[f"transcript_{version}"] = transcript == (expected["initial_input"] if version == 1 else expected["final_input"])
        say, effect = notification["result_text"], notification["mutation_success"]
        checks[f"result_after_effect_{version}"] = bool(say and effect and say["event"]["ts"] > effect["event"]["ts"])
        turns.append({"intent_version": version, "input": clip["label"], "mutation_tool": mutation,
                      "speech_onset_monotonic": start + clip["activity_start_sample"] / clip["sample_rate"] if start is not None else None, "speech_end_monotonic": end,
                      "transcript": transcript, "utterances": utterances, "speech_end_to_client_observation_ms": metrics})
    interruption = None
    if expected.get("followup_audio"):
        old = first(report["speech_handles"], lambda row: row.get("kind") == "result" and row.get("intent_version") == 1)
        following = sorted([row for row in report["speech_handles"] if row.get("intent_version") == 2], key=lambda row: row["queued_at"])
        checks["old_result_interruption_requested"] = bool(old and old["state"] == "interruption_requested")
        if old and following and len(turns) == 2 and turns[1]["speech_onset_monotonic"] is not None:
            onset = turns[1]["speech_onset_monotonic"]
            loud = [(index, row) for index, row in enumerate(frames) if old["queued_at"] <= row["received_monotonic"] < following[0]["queued_at"] and row["rms_db"] >= report["config"]["threshold_db"]]
            if loud:
                index, last = loud[-1]
                after = frames[index + 1] if index + 1 < len(frames) else None
                user = first(client, lambda row: row["event"]["type"] == "user_state" and row["event"]["data"].get("state") == "speaking" and row["observed_monotonic"] >= onset)
                interruption = {"old_handle": old, "last_old_frame_line": index + 1, "last_old_frame": last, "following_frame": after,
                    "last_old_pcm_after_input_onset_ms": round((last["received_monotonic"] - onset) * 1000, 3),
                    "user_speaking_after_input_onset_ms": round((user["observed_monotonic"] - onset) * 1000, 3) if user else None,
                    "handle_interruption_after_input_onset_ms": round((old["ended_at"] - onset) * 1000, 3) if old.get("ended_at") and old["state"] == "interruption_requested" else None,
                    "old_handle_end_after_input_onset_ms": round((old["ended_at"] - onset) * 1000, 3) if old.get("ended_at") else None,
                    "old_handle_state": old["state"],
                    "untagged_tail_after_recorder_last_ms": round((last["received_monotonic"] - old["last_loud_at"]) * 1000, 3) if old.get("last_loud_at") else None,
                    "next_utterance_queue_monotonic": following[0]["queued_at"]}
    probe = None
    if case in {"silence", "noise"}:
        probe_start = first(events, lambda row: row["kind"] == "input_started" and row["label"] == "probe")
        post = [row for row in server if row["ts"] >= probe_start["received_unix"]]
        obs = report["probe_observation"]
        duration = obs["observed_end_monotonic"] - obs["started_monotonic"]
        post_calls = [row for row in calls if row["timestamp_start"] >= probe_start["received_unix"]]
        probe = {"observation_seconds": duration, "deadline_overshoot_seconds": obs["observed_end_monotonic"] - obs["deadline_monotonic"], "original_handle": obs["original_handle"],
                 "post_probe_calls": post_calls, "post_probe_user_events": [row for row in post if row["type"] in {"user_final", "user_state"}]}
        checks.update(probe_window_complete=duration >= 12, no_post_probe_calls=not post_calls,
                      no_post_probe_final=not any(row["type"] == "user_final" for row in post), original_result_finished=obs["original_handle"]["state"] == "finished")
    checks["speech_handles_settled"] = bool(report["speech_handles"]) and all(row["state"] == ("interruption_requested" if expected.get("followup_audio") and row.get("kind") == "result" and row.get("intent_version") == 1 else "finished") and not row.get("error") for row in report["speech_handles"])
    return {"name": entry["name"], "case": case, "room": report["room"], "checks": checks, "calls": calls, "effects": effects,
            "trace": {"server_count": len(server), "client_count": len(received), "missing": missing, "extra": extra},
            "turns": turns, "interruption": interruption, "probe": probe, "speech_handles": report["speech_handles"],
            "server_error_events": [row for row in server if row["type"] in ERROR_TYPES], "capture_errors": {key: report.get(key, []) for key in ("receiver_errors", "cleanup_errors")},
            "room_departure": entry.get("room_departure"), "room_cleanup_logs": cleanup_logs,
            "raw_sha256": {**report.get("artifact_sha256", {}), "report.json": digest(directory / "report.json"), "server_trace": digest(trace_path)},
            "hash_mismatches": {"artifacts": artifact_bad, "capture_frozen_sources": capture_frozen_bad, "worker_sources": worker_bad},
            "frames": {"count": len(frames), "pcm_bytes": dict(offset), "invalid_lines": frame_errors}}


def main():
    output = BASE / "audit-v2.json"
    if output.exists():
        raise FileExistsError("audit-v2.json exists; preserve the existing audit")
    suite, protocol = load(BASE / "run-report.json"), load(BASE / "protocol.json")
    if not suite.get("finished_at") or suite["status"] in {"starting", "running"}:
        raise RuntimeError("suite must be terminal, including cleanup, before audit")
    shared = rows(STACK / "tool-calls.jsonl") if (STACK / "tool-calls.jsonl").exists() else []
    worker_logs = []
    for line in (STACK / "worker.log").read_text().splitlines() if (STACK / "worker.log").exists() else []:
        try:
            worker_logs.append(json.loads(line))
        except ValueError:
            pass
    counts, trials = Counter(), []
    for case in protocol["run_order"]:
        counts[case] += 1
        name = f"rtc-{case}-{counts[case]}"
        entry = first(suite["runs"], lambda row: row["name"] == name)
        try:
            if entry is None:
                raise FileNotFoundError("declared trial did not run")
            trials.append(audit_trial(entry, case, protocol["expected"][case], shared, worker_logs, suite["frozen_sha256"]))
        except Exception as error:
            trials.append({"name": name, "case": case, "checks": {"complete_raw_audit": False}, "audit_error": repr(error), "runner_entry": entry})
    check_names = sorted({key for row in trials for key in row["checks"]})
    rooms = {row["room"] for row in trials if "room" in row}
    summary = {"declared_trials": len(trials), "audited_trials": sum("audit_error" not in row for row in trials),
               "shared_attempts": len(shared), "successful_attempts": sum(row["call"].get("status") == "done" for row in shared),
               "attempt_status_counts": dict(Counter(row["call"].get("status", "missing") for row in shared)),
               "attempts_without_end": sum("timestamp_end" not in row["call"] for row in shared),
               "observed_effects": sum(len(row.get("effects", [])) for row in trials),
               "checks": {key: {"passed": sum(row["checks"].get(key) is True for row in trials), "evaluated": sum(key in row["checks"] for row in trials)} for key in check_names}}
    audit = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "All declared iteration12 raw captures, including prepared-but-unpublished inputs with null timings. Original audit.json KeyError is preserved; this corrected derivation changes no raw evidence or successful-trial metrics.",
             "summary": summary, "suite_status": suite["status"], "undeclared_runs": [row for row in suite["runs"] if row["name"] not in {trial["name"] for trial in trials}],
             "unassigned_attempts": [row for row in shared if row["room"] not in rooms], "failed_checks": [{"name": row["name"], "checks": [key for key, passed in row["checks"].items() if not passed]} for row in trials if not all(row["checks"].values())],
             "frozen_inputs_unchanged_at_end": suite.get("frozen_inputs_unchanged"), "current_frozen_input_mismatches": mismatch(ROOT, suite["frozen_sha256"]),
             "worker_cleanup": {"clean_exit": suite.get("worker_clean_exit"), "all_agents_departed": suite.get("all_agents_departed"), "records": suite["cleanup"], "drain_logs": [row for row in worker_logs if row.get("message") == "draining worker"]},
             "evidence_sha256": {str(path.relative_to(ROOT)): digest(path) for path in [Path(__file__), BASE / "protocol.json", BASE / "run-report.json", STACK / "worker.log", STACK / "tool-calls.jsonl"] if path.exists()},
             "limitations": ["Four serial repeated development cases with a shared warm stack; no general accuracy, physical playback, capacity or live slow-logging benefit claim.",
                 "Full trace equality failures are retained. Missing initial listening and server events after client disconnect are classified separately; wall-clock boundary ordering uses same-host clocks and is not exact transport attribution.",
                 "Latency uses client monotonic receipt versus scheduled 20 ms RMS-threshold input boundaries. Tool start is durable intention preparation, not confirmed backend entry. Ack and substantive response remain separate.",
                 "Per-turn mutation timing names the declared state-changing tool, including add_waypoint. The driver labels every triggered second utterance correction; waypoint requests add a stop without replacing the destination.",
                 "Old speech tail scans every above-threshold frame before the next utterance queue, including untagged tail. PCM/text association is heuristic; interruption_requested is not confirmed physical stop time.",
                 "Frame offsets verify saved PCM completeness, not receipt of every transmitted packet or drainage of SDK queues. No concatenation introduces missing wall-clock gaps.",
                 "Whole received-audio ASR and semantic speech review are separate pending artifacts; declared text plus PCM is not proof of intelligibility or complete content.",
                 "Agent absence, cleanup logs and worker exit are separate observations. No room deletion is used. Late server events after recorder exit remain in raw traces.",
                 "Malformed or missing trials remain in the declared denominator with audit_error; unassigned attempt rows are preserved. Runtime worker warnings require separate inspection."], "trials": trials}
    with output.open("x") as stream:
        json.dump(audit, stream, indent=2)
        stream.write("\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
