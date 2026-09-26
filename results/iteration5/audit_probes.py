"""Read-only derivation of the declared iteration 5 captures; no model loading."""
import collections
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "results/iteration5"


def read(path):
    return json.loads(path.read_text())


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mismatches(base, hashes):
    return [name for name, value in hashes.items() if sha(base / name) != value]


def bare(event):
    return {key: value for key, value in event.items() if key != "snapshot"}


def main():
    suite = read(BASE / "run-report.json")
    protocol = read(BASE / "protocol.json")
    shared_path = BASE / "local-stack-run1/tool-calls.jsonl"
    shared = lines(shared_path)
    asr_path = BASE / "received-audio-review/qualitative-review.json"
    asr = {row["id"]: row for row in read(asr_path)["trials"]}
    counts = collections.Counter()
    trials = []
    for entry in suite["runs"]:
        directory = BASE / entry["name"]
        report = read(directory / "report.json")
        events = lines(directory / "events.jsonl")
        frames = lines(directory / "frames.jsonl")
        trace_path = BASE / "local-stack-run1/traces" / (report["room"] + ".jsonl")
        server = lines(trace_path)
        client = [bare(row["event"]) for row in events if row["kind"] == "coordinator_event"]
        obs = report["probe_observation"]
        probe = next(row for row in events if row["kind"] == "input_started" and row["label"] == "probe")
        post = [row for row in server if row["ts"] >= probe["received_unix"]]
        calls = [row["call"] for row in shared if row["room"] == report["room"]]
        started = {row["data"]["execution_id"] + ":" + row["data"]["id"]: row for row in server if row["type"] == "tool_started"}
        done = {row["data"]["execution_id"] + ":" + row["data"]["id"]: row for row in server if row["type"] == "tool_done"}
        paired = len(calls) == len(started) == len(done) and all(
            call["attempt_id"] in started and call["attempt_id"] in done
            and call["function"] == started[call["attempt_id"]]["data"]["tool"] == done[call["attempt_id"]]["data"]["tool"]
            and call["args"] == started[call["attempt_id"]]["data"]["args"] == done[call["attempt_id"]]["data"]["args"]
            and call["status"] == "done" and done[call["attempt_id"]]["data"]["result"].get("status") == "success"
            for call in calls
        )
        writes = [row for row in done.values() if row["data"]["tool"] in {"start_navigation", "add_waypoint", "cancel_navigation"}]
        effects = [row["data"]["result"] for row in writes if row["data"]["result"].get("status") == "success" and not row["data"]["result"].get("already_active")]
        expected_effect = len(writes) == len(effects) == 1 and writes[0]["data"]["tool"] == "start_navigation" and effects[0]["destination_id"] == "P_AIRPORT" and effects[0]["route_active"] and effects[0]["eta_min"] == 88
        post_calls = [call for call in calls if call["timestamp_start"] >= probe["received_unix"]]
        trace_equal = client == server
        initial_only = len(server) == len(client) + 1 and server[0]["type"] == "agent_state" and server[0]["data"] == {"state": "listening"} and client == server[1:]
        user_final = [row for row in post if row["type"] == "user_final"]
        user_states = [row for row in post if row["type"] == "user_state"]
        failure_events = [row for row in server if row["type"] in {"error", "tool_error", "tool_failed"} or (row["type"] == "speech_handle" and (row["data"].get("error") or row["data"].get("state") == "failed"))]
        offsets = collections.defaultdict(int)
        frame_errors = []
        for index, frame in enumerate(frames):
            name = frame["pcm_file"]
            if frame["offset_bytes"] != offsets[name] or frame["size_bytes"] != frame["samples"] * 2:
                frame_errors.append(index)
            offsets[name] = frame["offset_bytes"] + frame["size_bytes"]
        frame_files_match = bool(frames) and not frame_errors and all((directory / name).stat().st_size == size for name, size in offsets.items())
        artifact_errors = mismatches(directory, report["artifact_sha256"])
        source_errors = mismatches(ROOT, report["source_sha256"])
        window = obs["observed_end_monotonic"] - obs["started_monotonic"]
        control = entry["case"] != "fragment"
        counts.update(captures=1, completed_observations=int(report["status"] == "completed_probe_observation" and entry["exit_code"] == 0), successful_attempts=sum(call["status"] == "done" for call in calls), airport_effects=int(expected_effect), paired_tool_traces=int(paired), attempts_after_probe=len(post_calls), trace_equal=int(trace_equal), initial_event_missing_only=int(initial_only), full_trace_failures=int(not trace_equal), frame_integrity_ok=int(frame_files_match), raw_hashes_ok=int(not artifact_errors), source_hashes_ok=int(not source_errors), controls=int(control), controls_finished_without_user_event=int(control and not user_final and not user_states and obs["original_handle"]["state"] == "finished"), fragments=int(not control), fragments_original_interruption_requested=int(not control and obs["original_handle"]["state"] == "interruption_requested"), receiver_or_cleanup_errors=len(report["receiver_errors"]) + len(report["cleanup_errors"]), coordinator_failure_events=len(failure_events))
        trials.append({
            "name": entry["name"], "case": entry["case"], "room": report["room"],
            "completed_observation": report["status"], "observation_seconds": window,
            "observation_deadline_overshoot_seconds": obs["observed_end_monotonic"] - obs["deadline_monotonic"],
            "trace_coverage": {"full_equality": trace_equal, "client_count": len(client), "server_count": len(server), "only_initial_listening_missing": initial_only, "missing_events": [row for row in server if row not in client], "extra_events": [row for row in client if row not in server]},
            "tool_attempts": calls, "all_attempts_pair_with_successful_server_events": paired,
            "expected_single_airport_effect": bool(expected_effect), "effects": effects,
            "attempts_after_probe": post_calls, "post_probe_tool_events": [row for row in post if row["type"].startswith("tool_")],
            "initial_final_transcripts": [row["data"] for row in server if row["type"] == "user_final" and row["ts"] < probe["received_unix"]],
            "post_probe_final_transcripts": user_final, "post_probe_user_states": user_states,
            "original_handle": obs["original_handle"], "speech_handles": report["speech_handles"],
            "current_intent_version": obs["current_intent_version"], "current_result_finished": obs["current_result_finished"],
            "current_response_handles_finished": all(handle["state"] == "finished" for handle in report["speech_handles"] if handle["intent_version"] == obs["current_intent_version"] and handle["kind"] == "response"),
            "failure_events": failure_events, "receiver_errors": report["receiver_errors"], "cleanup_errors": report["cleanup_errors"],
            "frames": {"count": len(frames), "pcm_bytes": dict(offsets), "contiguous_and_complete": frame_files_match, "invalid_indices": frame_errors},
            "artifact_hash_mismatches": artifact_errors, "source_hash_mismatches": source_errors,
            "evidence_sha256": {"report.json": sha(directory / "report.json"), "server_trace": sha(trace_path), **report["artifact_sha256"]},
            "received_audio_review": asr[entry["name"]],
        })
    frozen_bad = mismatches(ROOT, suite["frozen_sha256"])
    worker = next(row for row in suite["cleanup"] if row["role"] == "worker")
    logs = [json.loads(line) for line in (BASE / "local-stack-run1/worker.log").read_text().splitlines() if line.startswith("{")]
    drain = next(row for row in reversed(logs) if row.get("message") == "draining worker")
    audit = {
        "reviewed_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "Derived audit of every declared iteration 5 raw capture and room-filtered server/tool trace; no inference, source edits, exclusions or score changes.",
        "summary": dict(counts), "all_declared_trials_present_in_order": [row["case"] for row in suite["runs"]] == protocol["run_order"],
        "observation_seconds_min_max": [min(row["observation_seconds"] for row in trials), max(row["observation_seconds"] for row in trials)],
        "frozen_inputs_unchanged_at_suite_end": suite["frozen_inputs_unchanged"], "frozen_input_count": len(suite["frozen_sha256"]), "current_frozen_input_mismatches": frozen_bad,
        "evidence_sha256": {str(path.relative_to(ROOT)): sha(path) for path in [BASE / "run-report.json", BASE / "protocol.json", shared_path, asr_path, BASE / "received-audio-review/report.json", BASE / "local-stack-run1/worker.log", BASE / "run_suite.py", Path(__file__)]},
        "cleanup": {"records": suite["cleanup"], "worker_clean_exit": False, "runner_grace_seconds": 20, "sdk_drain_log": drain, "drain_log_to_worker_exit_seconds": (datetime.fromisoformat(worker["finished_at"]) - datetime.fromisoformat(drain["timestamp"])).total_seconds(), "interpretation": "Worker exceeded the runner's 20 second graceful budget and required SIGKILL (-9), while the SDK logged a 3600 second drain timeout. This shows incomplete graceful shutdown within the runner budget, not an indefinite hang. LLM exited -15; LiveKit exited 0.", "source_lines": {"results/iteration5/run_suite.py": [151, 154, 156], ".venv/lib/python3.11/site-packages/livekit/agents/worker.py": [977, 980, 986, 1003]}},
        "limitations": [
            "Observation completion is capture completion, not a twelve-of-twelve quality pass. Eight full trace equality checks fail, each solely because the first server listening event is missing at the client; subsequent events match exactly.",
            "All nine controls emit no post-probe user_state event. These controls do not exercise a demonstrated VAD false interruption or pause/resume recovery. No dedicated false-interruption/resume event is forwarded by this integration.",
            "The 180 ms fragment is speech. Nap/Nap/Now are exploratory recognized outcomes, not a non-speech hallucination score. The old result is interrupted; all three new response handles finish. current_result_finished=false refers to the absence of a new handle of kind result, not an unfinished clarification/response.",
            "Tool log rows are executor observations; successful navigation results provide the mock-backend effect evidence. Post-probe comparisons use client wall-clock sample at input_started versus same-host server/tool wall timestamps; they do not establish network arrival or physical speech onset.",
            "Observation duration is client monotonic time from probe queue drain to recorded observation end. Scheduling overshoot is retained. PCM offsets cover all received samples; this does not prove every transmitted packet arrived or establish physical playout/intelligibility.",
            "Whole-stream ASR review is separately sourced, with no human listening claim. It recognizes full airport/88-minute results for nine controls and omits original ETA for all three fragments; Kempegowda is consistently rendered Kempegauda where spoken in that review.",
            "Three dependent repeats per case, fixed interleaving and shared warm stack retain cache/order confounds; no benchmark or statistical generalization.",
            "Empty receiver/coordinator error lists do not mean no runtime warnings. See telemetry-stall-audit.json for the 613 ms event-loop warning and untagged transport errors; attribution is bounded there.",
        ], "trials": trials,
    }
    (BASE / "probe-audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps({"summary": audit["summary"], "observation_seconds_min_max": audit["observation_seconds_min_max"], "frozen_mismatches": frozen_bad}, indent=2))


if __name__ == "__main__":
    main()
