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


def interruption_pair(old_version, new_version, turns, report, client, frames):
    old = first(report["speech_handles"], lambda row: row.get("kind") == "result" and row.get("intent_version") == old_version)
    following = sorted([row for row in report["speech_handles"] if row.get("intent_version") == new_version], key=lambda row: row["queued_at"])
    onset = turns[new_version - 1]["speech_onset_monotonic"]
    result = {"from_intent_version": old_version, "to_intent_version": new_version, "old_handle": old,
              "old_handle_state": old.get("state") if old else None, "last_old_frame_line": None,
              "last_old_frame": None, "following_frame": None, "last_old_pcm_after_input_onset_ms": None,
              "user_speaking_after_input_onset_ms": None, "handle_interruption_after_input_onset_ms": None,
              "old_handle_end_after_input_onset_ms": None, "untagged_tail_after_recorder_last_ms": None,
              "next_utterance_queue_monotonic": following[0]["queued_at"] if following else None}
    if old and onset is not None and old.get("ended_at") is not None:
        delta = round((old["ended_at"] - onset) * 1000, 3)
        result["old_handle_end_after_input_onset_ms"] = delta
        if old["state"] == "interruption_requested":
            result["handle_interruption_after_input_onset_ms"] = delta
    if not old or not following or onset is None:
        result["measurement_limit"] = "Missing old handle, following utterance, or published followup timing."
        return result
    loud = [(index, row) for index, row in enumerate(frames)
            if old["queued_at"] <= row["received_monotonic"] < following[0]["queued_at"]
            and row["rms_db"] >= report["config"]["threshold_db"]]
    user = first(client, lambda row: row["event"]["type"] == "user_state"
                 and row["event"]["data"].get("state") == "speaking"
                 and onset <= row["observed_monotonic"] < following[0]["queued_at"])
    result["user_speaking_after_input_onset_ms"] = round((user["observed_monotonic"] - onset) * 1000, 3) if user else None
    if loud:
        index, last = loud[-1]
        result.update(last_old_frame_line=index + 1, last_old_frame=last,
                      following_frame=frames[index + 1] if index + 1 < len(frames) else None,
                      last_old_pcm_after_input_onset_ms=round((last["received_monotonic"] - onset) * 1000, 3),
                      untagged_tail_after_recorder_last_ms=round((last["received_monotonic"] - old["last_loud_at"]) * 1000, 3) if old.get("last_loud_at") else None)
    return result


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
    reused = [row for row in server if row["type"] == "tool_reused"]
    plans = [row for row in server if row["type"] == "plan_ready"]
    finals = [row for row in server if row["type"] == "user_final"]
    reuse_provenance = []
    for index, row in enumerate(server):
        if row["type"] != "tool_reused":
            continue
        data = row["data"]
        canonical = json.dumps([data["tool"], data["args"]], sort_keys=True)
        prior = [{"server_line": i + 1, "event": candidate} for i, candidate in enumerate(server[:index])
                 if candidate["type"] == "tool_done" and candidate["data"]["tool"] == "search_destination"
                 and candidate["data"]["result"].get("status") == "success"
                 and json.dumps([candidate["data"]["tool"], candidate["data"]["args"]], sort_keys=True) == canonical]
        reason_valid = "reason" not in data or data["reason"] == "same lookup in the same declared cache context"
        reuse_provenance.append({"server_line": index + 1, "event": row, "canonical_tool_args": canonical,
                                 "earlier_successful_searches": prior, "reason_valid_if_present": reason_valid,
                                 "verified": data["tool"] == "search_destination" and bool(prior) and reason_valid})
    identity = lambda row: row["data"]["execution_id"] + ":" + row["data"]["id"]
    paired = len(calls) == len(started) == len(done) and len({call.get("attempt_id") for call in calls}) == len(calls)
    paired = paired and all(sum(identity(row) == call.get("attempt_id") and row["data"]["tool"] == call["function"]
                               and row["data"]["args"] == call["args"] for row in collection) == 1
                               for call in calls for collection in (started, done))
    writes = [row for row in done if row["data"]["tool"] in {"start_navigation", "add_waypoint", "cancel_navigation"}]
    effects = [row for row in writes if is_effect(row)]
    offset, frame_errors = defaultdict(int), []
    for index, frame in enumerate(frames, 1):
        name = frame["pcm_file"]
        if frame["offset_bytes"] != offset[name] or frame["size_bytes"] != frame["samples"] * 2:
            frame_errors.append(index)
        offset[name] = frame["offset_bytes"] + frame["size_bytes"]
    artifact_bad = mismatch(directory, report.get("artifact_sha256", {}))
    capture_bad = [name for name, value in report["source_sha256"].items() if frozen.get(str(ROOT / name)) != value]
    worker_receipt = load(directory / "worker_manifest.json")
    worker_bad = [name for name, value in report["source_sha256"].items() if name.startswith("agent/")
                  and worker_receipt["source_sha256"].get(str(ROOT / name)) != value]
    cleanup_logs = [row for row in worker_logs if row.get("message") in
                    {f"room cleanup started: {report['room']}", f"room cleanup finished: {report['room']}"}]
    count = len(expected["turns"])
    clips = report["inputs"]
    versions = list(range(1, count + 1))
    checks = {
        "capture_completed": entry.get("exit_code") == 0 and report["status"] == "completed_observation",
        "raw_hashes": bool(report.get("artifact_sha256")) and not artifact_bad,
        "frozen_source_receipts": not capture_bad and not worker_bad,
        "full_trace_equality": received == server,
        "frame_integrity": bool(frames) and len(report["tracks"]) == 1 and not frame_errors and all((directory / name).stat().st_size == size for name, size in offset.items()),
        "no_capture_errors": not report.get("receiver_errors") and not report.get("cleanup_errors"),
        "no_server_tool_errors": not any(row["type"] in ERROR_TYPES for row in server),
        "no_stale_results": all(not row["data"].get("stale") for row in done),
        "agent_departed": bool(entry.get("room_departure", {}).get("agent_departed")),
        "room_cleanup_logged": [row["message"] for row in cleanup_logs] == [f"room cleanup started: {report['room']}", f"room cleanup finished: {report['room']}"],
        "declared_input_count": len(clips) == count,
        "declared_input_audio": len(clips) == count and all(clip["sha256"] == digest(BASE / "audio-assets" / turn["audio"]) for clip, turn in zip(clips, expected["turns"])),
        "declared_input_labels": [clip["label"] for clip in clips] == ["initial", *[f"followup_{i}" for i in range(1, count)]],
        "all_final_transcripts_exact": [(row["data"].get("intent_version"), row["data"].get("text")) for row in finals] == [(i, turn["text"]) for i, turn in enumerate(expected["turns"], 1)],
    }
    plan_checks = {
        "attempt_count_in_declared_range": expected["attempts_range"][0] <= len(calls) <= expected["attempts_range"][1],
        "attempt_event_pairs": paired,
        "all_attempts_done": bool(calls) and all(row.get("status") == "done" and "timestamp_end" in row for row in calls),
        "all_done_results_successful": bool(done) and all(row["data"]["result"].get("status") == "success" for row in done),
        "only_static_search_reused": all(row["data"]["tool"] == "search_destination" for row in reused),
        "static_search_reuse_provenance": all(row["verified"] for row in reuse_provenance),
        "no_unexpected_plan_turns": all(row["data"].get("intent_version") in versions for row in plans),
        "no_unexpected_operation_turns": all(row["data"].get("operation_id") in {f"turn-{v}" for v in versions} for row in started + done + reused),
    }
    semantic_checks = {"effect_count": len(effects) == count, "no_noop_or_extra_writes": len(writes) == len(effects) == count}
    turns, route_ids = [], []
    for version, declared in enumerate(expected["turns"], 1):
        clip = clips[version - 1] if version <= len(clips) else None
        start = clip.get("timing", {}).get("started_monotonic") if clip else None
        end = start + clip["activity_end_sample"] / clip["sample_rate"] if start is not None else None
        checks[f"input_published_{version}"] = bool(clip and start is not None and clip["timing"].get("published_samples") == clip["samples"])
        turn_events = [row for row in client if row["event"]["data"].get("intent_version") == version]
        turn_finals = [row for row in finals if row["data"].get("intent_version") == version]
        turn_plans = [row for row in plans if row["data"].get("intent_version") == version]
        commits = [row for row in server if row["type"] == "gate_committed" and row["data"].get("intent_version") == version]
        operation = [row for row in server if row["data"].get("operation_id") == f"turn-{version}"]
        turn_done = [row for row in operation if row["type"] == "tool_done"]
        turn_started = [row for row in operation if row["type"] == "tool_started"]
        turn_reused = [row for row in operation if row["type"] == "tool_reused"]
        turn_effects = [row for row in effects if row["data"].get("operation_id") == f"turn-{version}"]
        plan_calls = turn_plans[0]["data"]["calls"] if len(turn_plans) == 1 else []
        planned_tools = [call["tool"] for call in plan_calls]
        plan_checks[f"allowed_plan_{version}"] = len(turn_plans) == 1 and planned_tools in declared["allowed_planned_sequences"]
        plan_checks[f"committed_plan_{version}"] = len(commits) == 1 and commits[0]["data"]["calls"] == planned_tools
        planned_ids = [(call["id"], call["tool"]) for call in plan_calls]
        materialized = [(row["data"]["id"], row["data"]["tool"]) for row in turn_done + turn_reused]
        plan_checks[f"all_planned_calls_accounted_{version}"] = bool(plan_calls) and len(set(planned_ids)) == len(planned_ids) and Counter(materialized) == Counter(planned_ids)
        effect_declared = expected["effects"][version - 1]
        mutation = effect_declared["tool"]
        semantic_checks[f"effect_state_{version}"] = len(turn_effects) == 1 and turn_effects[0]["data"]["tool"] == mutation and all(
            key in turn_effects[0]["data"]["result"] and turn_effects[0]["data"]["result"][key] == value for key, value in effect_declared.items() if key != "tool")
        route_id = None
        if mutation == "start_navigation":
            computes = [row for row in turn_done if row["data"]["tool"] == "compute_route"]
            compute_starts = [row for row in turn_started if row["data"]["tool"] == "compute_route"]
            starts = [row for row in turn_started if row["data"]["tool"] == "start_navigation"]
            route_id = computes[0]["data"]["result"].get("route_id") if len(computes) == 1 else None
            fresh = (len(computes) == len(compute_starts) == len(starts) == len(turn_effects) == 1 and isinstance(route_id, str) and bool(route_id)
                and computes[0]["data"]["result"].get("status") == "success"
                and sum(call.get("attempt_id") == identity(compute_starts[0]) for call in calls) == 1
                and sum(call.get("attempt_id") == identity(starts[0]) for call in calls) == 1
                and starts[0]["data"]["args"].get("route_id") == route_id
                and turn_effects[0]["data"]["result"].get("route_id") == route_id
                and computes[0]["ts"] <= starts[0]["ts"]
                and not any(row["data"]["tool"] in {"compute_route", "start_navigation"} for row in turn_reused))
            semantic_checks[f"fresh_computed_route_{version}"] = fresh
            route_ids.append(route_id)
        notification = {"transcript": first(turn_events, lambda row: row["event"]["type"] == "user_final")}
        for kind in ("ack", "result"):
            notification[kind + "_text"] = first(turn_events, lambda row: row["event"]["type"] == "agent_say" and row["event"]["data"].get("kind") == kind)
        client_operation = [row for row in client if row["event"]["data"].get("operation_id") == f"turn-{version}"]
        notification["first_tool_intention"] = first(client_operation, lambda row: row["event"]["type"] == "tool_started")
        for event_type, key in (("tool_started", "mutation_intention"), ("tool_done", "mutation_success")):
            notification[key] = first(client_operation, lambda row: row["event"]["type"] == event_type and row["event"]["data"].get("tool") == mutation and (event_type != "tool_done" or is_effect(row["event"])))
        metrics = {key + "_ms": round((row["observed_monotonic"] - end) * 1000, 3) if row and end is not None else None for key, row in notification.items()}
        for kind in ("ack", "result"):
            handle = first(report["speech_handles"], lambda row: row.get("intent_version") == version and row.get("kind") == kind)
            pcm = first(frames, lambda row: handle and handle["queued_at"] <= row["received_monotonic"] <= handle.get("ended_at", float("inf")) and row["rms_db"] >= report["config"]["threshold_db"])
            metrics[f"first_{kind}_pcm_ms"] = round((pcm["received_monotonic"] - end) * 1000, 3) if pcm and end is not None else None
            if kind == "result":
                metrics["result_handle_finished_ms"] = round((handle["ended_at"] - end) * 1000, 3) if handle and handle["state"] == "finished" and end is not None else None
        checks[f"transcript_{version}"] = len(turn_finals) == 1 and turn_finals[0]["data"].get("text") == declared["text"]
        result_says = [row for row in turn_events if row["event"]["type"] == "agent_say" and row["event"]["data"].get("kind") == "result"]
        say, effect = notification["result_text"], notification["mutation_success"]
        checks[f"result_after_effect_{version}"] = len(result_says) == 1 and bool(say and effect and say["event"]["ts"] > effect["event"]["ts"])
        turn_attempt_ids = {identity(row) for row in turn_started}
        turns.append({"intent_version": version, "input": clip["label"] if clip else None, "mutation_tool": mutation,
            "expected_operation_id": f"turn-{version}", "observed_execution_ids": sorted({row["data"]["execution_id"] for row in turn_started + turn_done + turn_reused}),
            "speech_onset_monotonic": start + clip["activity_start_sample"] / clip["sample_rate"] if start is not None else None,
            "speech_end_monotonic": end, "transcript": turn_finals[0]["data"].get("text") if len(turn_finals) == 1 else None,
            "final_transcript_rows": turn_finals, "plan_rows": turn_plans, "planned_tools": planned_tools,
            "physical_calls": [call for call in calls if call.get("attempt_id") in turn_attempt_ids], "reused_rows": turn_reused,
            "effects": turn_effects, "computed_route_id": route_id,
            "utterances": [row["event"]["data"] for row in turn_events if row["event"]["type"] == "agent_say"],
            "speech_end_to_client_observation_ms": metrics})
    if expected.get("require_distinct_fresh_route_ids"):
        semantic_checks["distinct_fresh_route_ids"] = bool(route_ids) and all(route_ids) and len(set(route_ids)) == len(route_ids)
    interruptions = [interruption_pair(old, new, turns, report, client, frames) for old, new in expected["required_interruption_pairs"]]
    for pair in interruptions:
        checks[f"old_result_interrupted_{pair['from_intent_version']}_to_{pair['to_intent_version']}"] = pair["old_handle_state"] == "interruption_requested"
    result_handles = [row for row in report["speech_handles"] if row.get("kind") == "result"]
    checks["one_result_handle_per_turn"] = Counter(row.get("intent_version") for row in result_handles) == Counter(versions)
    checks["speech_handles_settled"] = bool(report["speech_handles"]) and all(
        row.get("intent_version") in versions and row.get("kind") in {"ack", "result"}
        and row["state"] == ("interruption_requested" if row.get("kind") == "result" and row.get("intent_version") < count else "finished")
        and not row.get("error") for row in report["speech_handles"])
    checks.update(plan_checks)
    checks.update(semantic_checks)
    return {"name": entry["name"], "case": case, "room": report["room"], "checks": checks,
        "semantic_checks": semantic_checks, "plan_attempt_checks": plan_checks, "calls": calls, "effects": effects, "reused_rows": reused,
        "reuse_provenance": reuse_provenance,
        "all_final_transcript_rows": finals, "expected_final_transcripts": [{"intent_version": i, "text": turn["text"]} for i, turn in enumerate(expected["turns"], 1)],
        "trace": {"server_count": len(server), "client_count": len(received), "missing": missing, "extra": extra},
        "turns": turns, "interruptions": interruptions, "speech_handles": report["speech_handles"],
        "server_error_events": [row for row in server if row["type"] in ERROR_TYPES],
        "capture_errors": {key: report.get(key, []) for key in ("receiver_errors", "cleanup_errors")},
        "capture_failure": {key: report.get(key) for key in ("status", "failed_stage", "error")},
        "room_departure": entry.get("room_departure"), "room_cleanup_logs": cleanup_logs,
        "raw_sha256": {**report.get("artifact_sha256", {}), "report.json": digest(directory / "report.json"), "server_trace": digest(trace_path)},
        "hash_mismatches": {"artifacts": artifact_bad, "capture_frozen_sources": capture_bad, "worker_sources": worker_bad},
        "frames": {"count": len(frames), "pcm_bytes": dict(offset), "invalid_lines": frame_errors}}


def main():
    output = BASE / "audit.json"
    if output.exists():
        raise FileExistsError("audit.json exists; preserve the existing audit")
    suite, protocol = load(BASE / "run-report.json"), load(BASE / "protocol.json")
    if not suite.get("finished_at") or suite["status"] in {"starting", "running"}:
        raise RuntimeError("suite must be terminal, including cleanup, before audit")
    shared = rows(STACK / "tool-calls.jsonl") if (STACK / "tool-calls.jsonl").exists() else []
    worker_logs = []
    for line in (STACK / "worker.log").read_text().splitlines():
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
    attempt_range = [sum(protocol["expected"][case]["attempts_range"][i] for case in protocol["run_order"]) for i in (0, 1)]
    summary = {"declared_trials": len(trials), "audited_trials": sum("audit_error" not in row for row in trials),
        "declared_turns": sum(len(protocol["expected"][case]["turns"]) for case in protocol["run_order"]),
        "declared_interruption_pairs": sum(len(protocol["expected"][case]["required_interruption_pairs"]) for case in protocol["run_order"]),
        "observed_interruption_requests": sum(pair["old_handle_state"] == "interruption_requested" for row in trials for pair in row.get("interruptions", [])),
        "expected_attempts_range": attempt_range, "shared_attempts": len(shared), "total_attempts_in_range": attempt_range[0] <= len(shared) <= attempt_range[1],
        "successful_attempts": sum(row["call"].get("status") == "done" for row in shared),
        "attempt_status_counts": dict(Counter(row["call"].get("status", "missing") for row in shared)),
        "attempts_without_end": sum("timestamp_end" not in row["call"] for row in shared),
        "reused_calls": sum(len(row.get("reused_rows", [])) for row in trials),
        "expected_effects": sum(len(protocol["expected"][case]["effects"]) for case in protocol["run_order"]),
        "observed_effects": sum(len(row.get("effects", [])) for row in trials),
        "semantic_cases_passed": sum(bool(row.get("semantic_checks")) and all(row["semantic_checks"].values()) for row in trials),
        "plan_attempt_cases_passed": sum(bool(row.get("plan_attempt_checks")) and all(row["plan_attempt_checks"].values()) for row in trials),
        "checks": {key: {"passed": sum(row["checks"].get(key) is True for row in trials), "evaluated": sum(key in row["checks"] for row in trials)} for key in check_names}}
    audit = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": f"All four declared {BASE.name} raw captures, ten turns and six adjacent interruption pairs. Semantic state, logical plans, physical attempts, lookup reuse and receipt timing are separate checks.",
        "summary": summary, "suite_status": suite["status"],
        "undeclared_runs": [row for row in suite["runs"] if row["name"] not in {trial["name"] for trial in trials}],
        "unassigned_attempts": [row for row in shared if row["room"] not in rooms],
        "suite_checks": {"all_declared_trials_audited": all("audit_error" not in row for row in trials),
            "no_unassigned_attempts": all(row["room"] in rooms for row in shared),
            "no_undeclared_runs": all(row["name"] in {trial["name"] for trial in trials} for row in suite["runs"]),
            "total_attempts_in_declared_range": attempt_range[0] <= len(shared) <= attempt_range[1]},
        "failed_checks": [{"name": row["name"], "checks": [key for key, passed in row["checks"].items() if not passed]} for row in trials if not all(row["checks"].values())],
        "frozen_inputs_unchanged_at_end": suite.get("frozen_inputs_unchanged"), "current_frozen_input_mismatches": mismatch(ROOT, suite["frozen_sha256"]),
        "worker_cleanup": {"clean_exit": suite.get("worker_clean_exit"), "all_agents_departed": suite.get("all_agents_departed"), "records": suite["cleanup"], "drain_logs": [row for row in worker_logs if row.get("message") == "draining worker"]},
        "evidence_sha256": {str(path.relative_to(ROOT)): digest(path) for path in [Path(__file__), BASE / "protocol.json", BASE / "run-report.json", STACK / "worker.log", STACK / "tool-calls.jsonl"] if path.exists()},
        "limitations": ["Four serial development rooms share warm services; no causal historical timing or held-out quality claim.",
            "All additional, duplicate, wrong-version and missing finalized transcripts fail exact declared sequence checks; no posthoc reindexing.",
            "Static search reuse is an observed logical call without a physical attempt. Every navigation turn requires its own successful compute and start, with matching distinct route IDs.",
            "Receipt latency uses original client monotonic times against scheduled PCM activity boundaries; no physical audible latency claim. Deferred attribution resolution has its own timestamps.",
            "Each adjacent interruption pair scans every above-threshold frame before the next utterance queue, including untagged tail. Interruption requested is not confirmed physical stop time.",
            "Prepared or missing input has null timing; an unmeasurable interruption remains a declared row with null metrics. Missing trials remain in the denominator.",
            "Frame integrity and event equality do not establish receipt of every transmitted RTP packet or drained native queues. Whole-stream ASR is separate.",
            "Application cleanup, server job status and worker exit remain separate observations."], "trials": trials}
    with output.open("x") as stream:
        json.dump(audit, stream, indent=2)
        stream.write("\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
