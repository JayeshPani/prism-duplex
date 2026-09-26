"""Extract every declared acoustic case without inference or automatic speech judging.

All source artifacts remain read-only. Missing/unrun/failed cases keep their rows;
received ASR and required speech facts remain separate from pending manual review.
"""
import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
MUTATIONS = {"start_navigation", "add_waypoint", "cancel_navigation"}
TERMINAL_TOOL_EVENTS = {"tool_done", "tool_error", "tool_cancelled", "tool_unknown", "tool_blocked"}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class Evidence:
    def __init__(self, root):
        self.root, self.hashes, self.issues = root, {}, []

    def name(self, path):
        return str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)

    def reference(self, path, required=True):
        path = Path(path)
        if not path.is_file():
            if required:
                self.issues.append({"path": self.name(path), "error": "missing_file"})
            return None
        try:
            value = digest(path)
            self.hashes[self.name(path)] = value
            return value
        except (OSError, ValueError) as error:
            self.issues.append({"path": self.name(path), "error": repr(error)})
            return None

    def load(self, path, required=True):
        if self.reference(path, required) is None:
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError) as error:
            self.issues.append({"path": self.name(path), "error": repr(error)})
            return None

    def rows(self, path, required=True):
        if self.reference(path, required) is None:
            return None
        result = []
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                result.append({"line": number, "row": json.loads(line)})
            except ValueError as error:
                self.issues.append({"path": self.name(path), "line": number, "raw": line, "error": repr(error)})
        return result


def event_only(event):
    return {key: event[key] for key in ("type", "data", "ts")}


def identity(event):
    data = event["data"]
    return f"{data.get('execution_id')}:{data.get('id')}"


def difference(left, right):
    available = Counter(json.dumps(row, sort_keys=True) for row in right)
    missing = []
    for number, row in enumerate(left, 1):
        key = json.dumps(row, sort_keys=True)
        if available[key]:
            available[key] -= 1
        else:
            missing.append({"event_index": number, "event": row})
    return missing


def changed_navigation(event):
    data = event["data"]
    result = data.get("result", {})
    if event["type"] != "tool_done" or result.get("status") != "success":
        return False
    return ((data.get("tool") == "start_navigation" and result.get("already_active") is False)
            or (data.get("tool") == "add_waypoint" and result.get("already_present") is False)
            or (data.get("tool") == "cancel_navigation" and bool(result.get("cancelled"))))


def criterion(value, detail):
    return {"status": "unmeasured" if value is None else "passed" if value else "failed", "detail": detail}


def worker_package_check(root, arm, worker, selection, frozen):
    policy = arm.name.removeprefix("iteration20-")
    package = root / "results/iteration20/baseline-package" if policy == "baseline" else root
    agent = package / "agent"
    if worker is None or selection is None or not frozen:
        return criterion(None, {"selected_package": str(package), "reason": "Missing worker manifest, selection or run freeze"})
    expected = {p: h for p, h in frozen.items() if Path(p).is_relative_to(agent)
                and (Path(p).suffix == ".py" or Path(p).name == "config.yaml")}
    manifest = worker.get("source_sha256", {})
    manifest_agent = {p: h for p, h in manifest.items() if any(Path(p).is_relative_to(base) for base in
        (root / "agent", root / "results/iteration20/baseline-package/agent"))}
    loaded = selection.get("loaded_agent_modules", {})
    module_matches = {name: ((name == "agent" or name.startswith("agent."))
        and Path(row.get("path", "")).is_relative_to(agent)
        and row.get("sha256") == expected.get(row.get("path"))) for name, row in loaded.items()}
    checks = {"selected_policy": policy in {"baseline", "candidate"} and selection.get("policy") == policy,
        "selected_package": selection.get("agent_package_root") == str(package),
        "complete_manifest_agent_inventory": bool(expected) and manifest_agent == expected,
        "all_manifest_sources_frozen": bool(manifest) and all(frozen.get(p) == h for p, h in manifest.items()),
        "loaded_agent_modules_match": bool(loaded) and {"agent", "agent.main"} <= loaded.keys()
            and all(module_matches.values())}
    return criterion(all(checks.values()), {"selected_package": str(package), "checks": checks,
        "loaded_module_matches": module_matches,
        "limit": "Import/source provenance only; inference and final worker cleanup are checked separately."})


def input_times(clip, events):
    timing = clip.get("timing", {})
    start, rate = timing.get("started_monotonic"), clip.get("sample_rate")
    result = {"start_monotonic": start, "end_monotonic": timing.get("drained_monotonic"),
              "activity_onset_monotonic": None, "activity_end_monotonic": None, "start_unix": None}
    if start is not None and rate:
        for source, target in (("activity_start_sample", "activity_onset_monotonic"),
                               ("activity_end_sample", "activity_end_monotonic")):
            if clip.get(source) is not None:
                result[target] = start + clip[source] / rate
        matches = [x["row"] for x in events if x["row"].get("kind") == "input_started"
                   and x["row"].get("label") == clip["label"]
                   and x["row"].get("at_monotonic") == start]
        if len(matches) == 1:
            row = matches[0]
            result["start_unix"] = row["received_unix"] + start - row["received_monotonic"]
    return result


def pcm_timing(frames, handles, old, onset, threshold):
    """Keep heuristic old-audio windows, gaps and later returns, never just flags."""
    output = {"old_handle": old, "input_onset_monotonic": onset,
              "old_handle_end_after_onset_ms": None, "interruption_request_after_onset_ms": None,
              "last_old_pcm_after_onset_ms": None, "quiet_intervals": [], "loud_runs": []}
    if old is None or onset is None:
        output["unmeasured_reason"] = "Missing trigger handle or published input onset"
        return output
    if old.get("ended_at") is not None:
        output["old_handle_end_after_onset_ms"] = round((old["ended_at"] - onset) * 1000, 3)
        if old.get("state") == "interruption_requested":
            output["interruption_request_after_onset_ms"] = output["old_handle_end_after_onset_ms"]
    following = [h["queued_at"] for h in handles if h.get("speech_id") != old.get("speech_id")
                 and h.get("queued_at", -1) > old.get("queued_at", -1)]
    boundary = min(following) if following else None
    window = [x for x in frames if old.get("queued_at", onset) <= x["row"]["received_monotonic"]
              and (boundary is None or x["row"]["received_monotonic"] < boundary)]
    output["next_speech_queue_monotonic"] = boundary
    output["window_frame_lines"] = [window[0]["line"], window[-1]["line"]] if window else None
    output["attribution_limit"] = "Single-agent event-order/quiet-boundary window ends at next utterance queue; untagged late PCM is retained. RTP/data receipt ordering is heuristic, not physical speaker timing."
    loud = [x for x in window if x["row"].get("rms_db", -999) >= threshold]
    if loud:
        output["last_old_pcm_after_onset_ms"] = round((loud[-1]["row"]["received_monotonic"] - onset) * 1000, 3)
        output["last_old_pcm_line"] = loud[-1]["line"]
    current = None
    for item in window:
        row = item["row"]
        if row.get("rms_db", -999) >= threshold:
            if current is None:
                current = {"first_line": item["line"], "start_monotonic": row["received_monotonic"]}
            current.update(last_line=item["line"], end_monotonic=row["received_monotonic"])
        elif current is not None:
            output["loud_runs"].append(current)
            current = None
    if current is not None:
        output["loud_runs"].append(current)
    # Only consecutive observed quiet frames support a quiet interval; missing
    # network frames cannot themselves prove silence.
    quiet = None
    for item in window:
        row = item["row"]
        at = row["received_monotonic"]
        if at < onset:
            continue
        if row.get("rms_db", -999) < threshold:
            if quiet is None or at - quiet["last_monotonic"] > .1:
                if quiet is not None:
                    output["quiet_intervals"].append(quiet)
                quiet = {"first_line": item["line"], "start_monotonic": at}
            quiet.update(last_line=item["line"], last_monotonic=at, duration_ms=round((at - quiet["start_monotonic"]) * 1000, 3))
        elif quiet is not None:
            output["quiet_intervals"].append(quiet)
            quiet = None
    if quiet is not None:
        output["quiet_intervals"].append(quiet)
    sustained = next((q for q in output["quiet_intervals"] if q["duration_ms"] >= 300), None)
    output["first_300ms_quiet_after_onset_ms"] = round((sustained["start_monotonic"] - onset) * 1000, 3) if sustained else None
    output["loud_returns_after_first_quiet"] = [r for r in output["loud_runs"]
        if sustained and r["start_monotonic"] > sustained["last_monotonic"]]
    return output


def effect_checks(expected, server, calls):
    writes = [e for e in server if e["type"] == "tool_done" and e["data"].get("tool") in MUTATIONS]
    effects = [e for e in writes if changed_navigation(e)]
    declared = expected["effects"]
    checks = {"effect_count": criterion(len(effects) == len(declared), {"expected": len(declared), "observed": len(effects)}),
              "no_extra_navigation_attempts": criterion(sum(c.get("function") in MUTATIONS for c in calls) == len(declared),
                  "Every physical navigation attempt remains counted, including failed/no-op writes."),
              "no_stale_completed_navigation": criterion(all(not e["data"].get("stale") for e in writes), "Tool-done stale flags, separate from attempts.")}
    if 'allowed_cancel_attempts' in expected:
        counts = Counter(c.get('function') for c in calls if c.get('function') in MUTATIONS)
        no_op_cancels = [e for e in writes if e['data'].get('tool') == 'cancel_navigation']
        allowed = (not declared and counts['cancel_navigation'] in expected['allowed_cancel_attempts']
                   and all(counts[name] == 0 for name in expected['forbidden_attempts']))
        checks['no_extra_navigation_attempts'] = criterion(allowed,
            {'observed_attempt_counts': dict(counts), 'allowed_cancel_attempts': expected['allowed_cancel_attempts'],
             'limit': 'All attempts retained. Explicit stop may issue one no-op cancellation in a fresh inactive room.'})
        checks['cancellation_outcome_grounded'] = criterion(
            len(no_op_cancels) == counts['cancel_navigation'] and all(
                e['data'].get('result', {}).get('status') == 'success'
                and e['data']['result'].get('route_active') is False
                and e['data']['result'].get('navigation_version') == 0
                and 'cancelled' in e['data']['result'] and e['data']['result']['cancelled'] is None
                for e in no_op_cancels),
            {'successful_cancel_rows': no_op_cancels,
             'limit': 'With zero cancel attempts, inactive state follows from fresh-room initial state and no navigation writes; no direct final snapshot is claimed.'})
    if 'required_read' in expected:
        required = expected['required_read']
        matching = [e for e in server if e['type'] == 'tool_done'
                    and e['data'].get('tool') == required['tool'] and not e['data'].get('stale')
                    and e['data'].get('result', {}).get('status') == 'success'
                    and all(e['data']['result'].get(k) == v for k, v in required.items() if k != 'tool')
                    and sum(c.get('attempt_id') == identity(e) and c.get('function') == required['tool']
                            and c.get('status') == 'done' for c in calls) == 1]
        checks['required_read_grounded'] = criterion(bool(matching),
            {'expected': required, 'matching_events': matching,
             'limit': 'Successful nonstale result with matching completed physical attempt identity; all other rows remain retained.'})
    previous = None
    for index, specification in enumerate(declared):
        event = effects[index] if index < len(effects) else None
        result = event["data"].get("result", {}) if event else {}
        good = event is not None and event["data"].get("tool") == specification["tool"]
        fields = {key: value for key, value in specification.items() if key not in {"tool", "cancelled_destination_id"}}
        if specification["tool"] == "start_navigation":
            fields.update(route_active=True, navigation_version=index + 1, stop_ids=[], replaced=None, already_active=False)
        good = good and all(result.get(key) == value and key in result for key, value in fields.items())
        if "cancelled_destination_id" in specification:
            good = good and bool(previous and previous.get("destination_id") == specification["cancelled_destination_id"]
                                 and result.get("cancelled") == previous.get("destination"))
        checks[f"effect_{index + 1}_state"] = criterion(good, {"expected": specification, "observed": event})
        if event and specification["tool"] == "start_navigation":
            data = event["data"]
            computes = [e for e in server if e["type"] == "tool_done" and e["data"].get("tool") == "compute_route"
                        and e["data"].get("execution_id") == data.get("execution_id")
                        and e["data"].get("result", {}).get("status") == "success"
                        and e["data"]["result"].get("route_id") == result.get("route_id")
                        and e["data"]["result"].get("destination_id") == result.get("destination_id")]
            starts = [e for e in server if e["type"] == "tool_started" and identity(e) == identity(event)]
            fresh = (len(computes) == len(starts) == 1 and computes[0]["ts"] <= starts[0]["ts"]
                     and starts[0]["data"].get("args", {}).get("route_id") == result.get("route_id")
                     and sum(c.get("attempt_id") == identity(computes[0]) for c in calls) == 1)
            checks[f"effect_{index + 1}_fresh_compute_dependency"] = criterion(fresh, "Successful compute and start in the same execution, matching target/route, physical compute attempt and dispatch order.")
        elif specification["tool"] == "start_navigation":
            checks[f"effect_{index + 1}_fresh_compute_dependency"] = criterion(False, "Expected start effect unavailable")
        if event:
            previous = result
    return checks, effects, writes


def audit_case(root, arm, case, suite, shared, stt, delivery, asr, evidence, assets, result=None):
    name = f"rtc-{case['id']}-1"
    directory = arm / name
    entries = [r for r in (suite or {}).get("runs", []) if r.get("name") == name]
    result = {} if result is None else result
    result.update({"case_id": case["id"], "trial": name, "declaration": case, "run_entries": entries,
              "status": "unrun" if not entries and not directory.exists() else "extraction_pending",
              "criteria": {}, "speech_review": {"status": "pending_manual_review", "required_facts": case["expected"].get("final_speech", []),
                  "initial_required_facts": case["expected"].get("initial_speech", []),
                  "received_asr_rows": [r for r in (asr or {}).get("trials", []) if r.get("id") == name],
                  "automatic_semantic_judgment": None}})
    issue_start = len(evidence.issues)
    report = evidence.load(directory / "report.json")
    if report is None:
        result["status"] = "unrun" if not entries else "missing_capture_report"
        result["missing_evidence"] = evidence.issues[issue_start:]
        return result
    result["capture_report"] = report
    journal = evidence.rows(directory / "events.jsonl")
    frames = evidence.rows(directory / "frames.jsonl")
    server_rows = evidence.rows(arm / "local-stack-run1/traces" / f"{report['room']}.jsonl")
    journal, frames = journal or [], frames or []
    result["other_capture_journal_rows"] = [x for x in journal if x["row"].get("kind") not in {"data_packet", "coordinator_event"}]
    server = [x["row"] for x in (server_rows or [])]
    promoted = [{"line": x["line"], **x["row"], "event": event_only(x["row"]["event"])}
                for x in journal if x["row"].get("kind") == "coordinator_event"]
    raw, decoded, decode_errors = [], [], []
    for entry in journal:
        row = entry["row"]
        if row.get("kind") != "data_packet":
            continue
        item = {"line": entry["line"], **{k: v for k, v in row.items() if k != "payload_base64"}}
        try:
            payload = base64.b64decode(row["payload_base64"], validate=True)
            item["payload_sha256"] = hashlib.sha256(payload).hexdigest()
            if row.get("topic") == "agent-events":
                item["decoded_event"] = event_only(json.loads(payload))
                decoded.append(item["decoded_event"])
        except (ValueError, KeyError, TypeError) as error:
            item["decode_error"] = repr(error)
            decode_errors.append(item)
        raw.append(item)
    dispositions = [{"line": x["line"], **x["row"]} for x in journal if x["row"].get("kind") == "data_attribution"]
    by_packet = {p["packet_id"]: p for p in raw}
    trust_errors = []
    for row in promoted:
        packet = by_packet.get(row.get("packet_id"))
        sender = packet.get("native_participant_identity") if packet else None
        if packet and sender is None:
            sender = packet.get("participant")
        if (not isinstance(sender, str) or not sender.strip() or sender != row.get("participant")
                or packet.get("decoded_event") != row["event"]
                or any(packet.get(k) != row.get(k) for k in ("received_monotonic", "received_unix"))
                or row.get("observed_monotonic") != packet.get("received_monotonic")
                or not any(d.get("packet_id") == row.get("packet_id") and d.get("status") == "promoted" and d.get("identity") == sender for d in dispositions)):
            trust_errors.append(row)
    result["traces"] = {"server": server_rows, "raw_packet_metadata_and_decoded_events": raw,
        "promoted": promoted, "dispositions": dispositions, "raw_decode_errors": decode_errors,
        "missing_from_promoted": difference(server, [r["event"] for r in promoted]),
        "extra_promoted": difference([r["event"] for r in promoted], server),
        "missing_from_raw": difference(server, decoded), "extra_raw": difference(decoded, server),
        "trust_errors": trust_errors,
        "participant_records": [x for x in journal if x["row"].get("kind") in {"participant_connected", "track_subscribed"}],
        "boundary": "Raw decoded equality does not authenticate anonymous packets. Full raw base64/snapshots remain in the hashed journal; this extraction retains every packet reference, metadata and coordinator event."}
    checks = result["criteria"]
    checks["one_declared_run"] = criterion(len(entries) == 1, "Repeated or missing launches remain visible")
    checks["capture_completed"] = criterion(len(entries) == 1 and entries[0].get("exit_code") == 0
        and report.get("status") in {"completed_observation", "completed_probe_observation"}, "Capture completion alone is not quality success")
    checks["trusted_trace_equality"] = criterion([r["event"] for r in promoted] == server if server_rows else None, "Full ordered promoted/server events; no initial-event exclusion")
    checks["raw_trace_equality"] = criterion(decoded == server and not decode_errors if server_rows else None, "Full ordered raw/server events, including anonymous packets")
    checks["promoted_identity_consistent"] = criterion(not trust_errors and len({r.get("packet_id") for r in promoted}) == len(promoted) if promoted else None,
        "Checks transport identity/disposition/bytes/timestamps; mapping source and SDK adapter are recorded separately")
    checks["capture_errors_absent"] = criterion(not report.get("receiver_errors") and not report.get("cleanup_errors"), "All receiver/cleanup errors preserved")
    checks["agent_departed"] = criterion(entries[0].get("room_departure", {}).get("agent_departed") if len(entries) == 1 else None, "Recorded per-room departure observation")
    calls = [row["row"]["call"] for row in (shared or []) if row["row"].get("room") == report["room"]]
    types = lambda names: [e for e in server if e["type"] in names]
    result["execution"] = {"physical_calls": calls, "physical_status_counts": dict(Counter(c.get("status", "missing") for c in calls)),
        "plans_and_repairs": types({"plan_ready", "plan_rejected", "gate_committed"}),
        "tool_events": [e for e in server if e["type"].startswith("tool_")],
        "read_errors": [e for e in types({"tool_error", "tool_done"}) if e["data"].get("tool") not in MUTATIONS
                        and (e["type"] == "tool_error" or e["data"].get("result", {}).get("status") == "error")],
        "reused_and_blocked": types({"tool_reused", "tool_blocked"})}
    pairing = []
    for call in calls:
        starts = [e for e in server if e["type"] == "tool_started" and identity(e) == call.get("attempt_id")]
        endings = [e for e in server if e["type"] in TERMINAL_TOOL_EVENTS and identity(e) == call.get("attempt_id")]
        pairing.append({"attempt_id": call.get("attempt_id"), "start_events": starts, "terminal_events": endings,
                        "single_start_matching_arguments": len(starts) == 1 and starts[0]["data"].get("tool") == call.get("function") and starts[0]["data"].get("args") == call.get("args")})
    result["execution"]["attempt_event_pairing"] = pairing
    checks["trace_attempt_inventory"] = criterion(Counter(identity(e) for e in server if e["type"] == "tool_started")
        == Counter(c.get("attempt_id") for c in calls) if shared is not None and server_rows else None,
        "Every trace start and physical attempt is retained, including retries and errors")
    checks["physical_attempts_have_matching_start"] = criterion(all(r["single_start_matching_arguments"] for r in pairing)
        and len({c.get("attempt_id") for c in calls}) == len(calls) if shared is not None and server_rows else None, "Endings/errors retained without assuming every attempt succeeded")
    if server_rows and shared is not None:
        result["effect_criteria"], effects, writes = effect_checks(case["expected"], server, calls)
    else:
        result["effect_criteria"], effects, writes = {"effects_measurable": criterion(None, "Missing server trace or shared attempts")}, [], []
    result["execution"].update(successful_mutations=effects, completed_navigation_rows=writes)
    offsets, frame_errors = defaultdict(int), []
    for item in frames:
        row = item["row"]
        if row["offset_bytes"] != offsets[row["pcm_file"]] or row["size_bytes"] != row["samples"] * 2:
            frame_errors.append(item["line"])
        offsets[row["pcm_file"]] = row["offset_bytes"] + row["size_bytes"]
    pcm_files = {name: {"sha256": evidence.reference(directory / name), "size_bytes": (directory / name).stat().st_size if (directory / name).is_file() else None}
                 for name in offsets}
    checks["frame_integrity"] = criterion(bool(frames) and len(report.get("tracks", [])) == 1 and not frame_errors
        and all(pcm_files[name]["size_bytes"] == size for name, size in offsets.items()), "Contiguous PCM16 offsets/sample counts and final file sizes")
    result["pcm"] = {"frame_count": len(frames), "frame_integrity_error_lines": frame_errors,
                     "files": pcm_files, "raw_frame_source": evidence.name(directory / "frames.jsonl")}
    artifact_matches = {name: evidence.reference(directory / name) == value for name, value in report.get("artifact_sha256", {}).items()}
    checks["capture_artifact_hashes"] = criterion(all(artifact_matches.values()) if artifact_matches else None, artifact_matches)
    worker = evidence.load(directory / "worker_manifest.json")
    selection = evidence.load(arm / "local-stack-run1/experiment-worker-selection.json")
    frozen = (suite or {}).get("frozen_sha256", {})
    sources = report.get("source_sha256", {})
    source_matches = {name: value == frozen.get(str(root / name), frozen.get(name)) for name, value in sources.items()}
    worker_matches = {name: value == (worker or {}).get("source_sha256", {}).get(str(root / name), (worker or {}).get("source_sha256", {}).get(name))
                      for name, value in sources.items() if name.startswith("agent/")}
    result["source_identity"] = {"capture_source_sha256": sources, "capture_vs_run_freeze": source_matches,
        "capture_agent_vs_worker": worker_matches,
        "capture_agent_vs_worker_role": "Informational: recorder hashes root agent sources; the baseline worker intentionally imports its separately frozen package.",
        "worker_manifest": worker, "worker_selection": selection, "sdk_identity_adapter": report.get("rtc_identity_adapter")}
    checks["capture_matches_run_freeze"] = criterion(all(source_matches.values()) if source_matches and frozen else None, "Uses arm run freeze, not today's working source")
    checks["worker_matches_selected_frozen_package"] = worker_package_check(root, arm, worker, selection, frozen)
    clips = report.get("inputs", [])
    result["inputs"] = []
    for index, declaration in enumerate(case["inputs"]):
        clip = clips[index] if index < len(clips) else None
        asset = root / "results/iteration20/audio-assets" / declaration["asset"]
        times = input_times(clip, journal) if clip else {}
        expected_hash = evidence.reference(asset)
        result["inputs"].append({"declaration": declaration, "capture": clip, "times": times,
                                "expected_asset_sha256": expected_hash})
        checks[f"input_{index + 1}_bytes"] = criterion(clip.get("sha256") == expected_hash if clip and expected_hash else None, "Exact declared WAV bytes")
        checks[f"input_{index + 1}_published"] = criterion(bool(clip and clip.get("timing", {}).get("published_samples") == clip.get("samples")
            and clip.get("timing", {}).get("started_monotonic") is not None), "Prepared but unplayed inputs do not pass")
    checks["declared_input_inventory"] = criterion(len(clips) == len(case["inputs"]), "No input or extra publication is silently excluded")
    result["extra_inputs"] = clips[len(case["inputs"]):]
    result["input_boundary_events"] = [x for x in journal if x["row"].get("kind") in {"input_started", "input_drained", "input_stopped_early", "input_trigger_wait", "input_trigger_failed"}
                                      or x["row"].get("kind", "").endswith("_trigger")]
    result["live_transcripts_and_merges"] = types({"user_final", "user_partial", "turn_merged"})
    result["spoken_text_and_handles"] = {"agent_say": types({"agent_say"}), "speech_handle_events": types({"speech_handle"}), "capture_handles": report.get("speech_handles", [])}
    result["vad_sdk_and_recognition_events"] = types({"user_state", "agent_state", "agent_false_interruption", "recognition_pause"})
    result["stt_diagnostics"] = {"calls": [r for r in (stt or {}).get("calls", []) if r.get("room") == report["room"]],
                                 "requests": [r for r in (stt or {}).get("requests", []) if r.get("room") == report["room"]]}
    result["delivery_diagnostics"] = {"bindings": [r for r in (delivery or {}).get("bindings", []) if r.get("room") == report["room"]],
        "calls": [r for r in (delivery or {}).get("calls", []) if r.get("room") == report["room"]],
        "policy": (delivery or {}).get("policy"), "binding_errors": (delivery or {}).get("binding_errors"),
        "source_hashes_unchanged": (delivery or {}).get("source_hashes_unchanged")}
    # Clarification and pause constraints use input publication receipts on the
    # same host; segment counts are observations, never assumed equal to inputs.
    if case["expected"].get("initial_mutations") == 0:
        boundary = result["inputs"][1]["times"].get("start_unix") if len(result["inputs"]) > 1 else None
        attempted = [c for c in calls if c.get("function") in MUTATIONS and boundary is not None and c.get("timestamp_start", float("inf")) < boundary]
        result["effect_criteria"]["no_navigation_attempt_before_clarifier"] = criterion(not attempted if boundary is not None else None, {"clarifier_start_unix": boundary, "premature_attempts": attempted})
    final_start = result['inputs'][-1]['times'].get('start_unix') if result['inputs'] else None
    if 'required_read' in case['expected']:
        matches = result['effect_criteria'].get('required_read_grounded', {}).get('detail', {}).get('matching_events', [])
        current_reads = [e for e in matches if final_start is not None and any(
            c.get('attempt_id') == identity(e) and c.get('timestamp_start', -1) >= final_start for c in calls)]
        result['effect_criteria']['required_read_after_final_input'] = criterion(
            bool(current_reads) if final_start is not None else None,
            {'final_input_start_unix': final_start, 'current_read_events': current_reads,
             'limit': 'Physical dispatch intention after the final query begins, in addition to the declared successful target/metric read.'})
    if case['expected'].get('no_navigation_attempt_after_final_query'):
        attempted = [c for c in calls if c.get('function') in MUTATIONS and final_start is not None
                     and c.get('timestamp_start', -1) >= final_start]
        result['effect_criteria']['no_navigation_attempt_after_final_query'] = criterion(
            not attempted if final_start is not None else None,
            {'final_input_start_unix': final_start, 'attempts_after_query': attempted,
             'limit': 'Prior active-state snapshot plus no subsequent mutation; this is not a direct final-state read.'})
    if case["expected"].get("writes_before_tail_activity") == 0:
        declaration = case["inputs"][0]
        metadata = assets.get(declaration["asset"], {})
        sample = declaration.get("tail_activity_start_sample", metadata.get("tail_activity_start_sample"))
        clip = clips[0] if clips else None
        start = result["inputs"][0]["times"].get("start_unix")
        boundary = start + sample / clip["sample_rate"] if start is not None and sample is not None and clip else None
        attempted = [c for c in calls if c.get("function") in MUTATIONS and boundary is not None and c.get("timestamp_start", float("inf")) < boundary]
        result["effect_criteria"]["no_write_before_continuation"] = criterion(not attempted if boundary is not None else None,
            {"tail_activity_start_sample": sample, "boundary_unix": boundary, "premature_attempts": attempted, "limit": "Needs frozen asset tail sample metadata; inserted gap alone is insufficient."})
    if len(case["inputs"]) == 2 and case["inputs"][1]["trigger"].startswith("first_fresh_current_result"):
        probe = result["inputs"][1]
        times = probe["times"]
        onset = times.get("activity_onset_monotonic")
        if onset is None:
            onset = times.get("start_monotonic")  # Silence has no voiced onset.
        trigger = report.get("probe_trigger", {})
        old = next((h for h in report.get("speech_handles", []) if h.get("speech_id") == trigger.get("speech_id")), None)
        result["probe_pcm_timing"] = pcm_timing(frames, report.get("speech_handles", []), old, onset, report["config"]["threshold_db"])
        after = [r for r in promoted if onset is not None and r.get("observed_monotonic", -1) >= onset]
        vad = [r for r in after if r["event"]["type"] == "user_state" and r["event"]["data"].get("state") == "speaking"]
        finals = [r for r in after if r["event"]["type"] == "user_final" and r["event"]["data"].get("text", "").strip()]
        onset_unix = (times["start_unix"] + onset - times["start_monotonic"]
                      if times.get("start_unix") is not None and onset is not None else None)
        server_finals = [e for e in server if e["type"] == "user_final" and e["data"].get("text", "").strip()
                         and onset_unix is not None and e["ts"] >= onset_unix]
        result["probe_observations"] = {"onset_basis": "activity sample if present, otherwise publication start", "post_probe_vad_speaking": vad,
            "post_probe_nonempty_final": finals, "post_probe_events": after,
            "onset_unix": onset_unix, "post_probe_server_nonempty_final": server_finals,
            "sdk_false_interruption_events": [r for r in after if r["event"]["type"] == "agent_false_interruption"],
            "recovery_exercised": "unexercised_no_observed_vad" if not vad else "VAD_observed_pause_resume_requires_timeline_review"}
        intentional = "playback_interruption" in case.get("category", [])
        checks["probe_trigger_has_original_result"] = criterion(bool(old and trigger.get("kind") == "result"), "Original speech ID frozen at probe trigger")
        checks["old_handle_outcome"] = criterion(old.get("state") == ("interruption_requested" if intentional else "finished") if old else None,
            "Handle outcome is separate from received-PCM cutoff and final content")
        if intentional:
            timing = result["probe_pcm_timing"]
            measured = timing.get("first_300ms_quiet_after_onset_ms")
            tail = timing.get("last_old_pcm_after_onset_ms")
            checks["voiced_stop_received_cutoff_screen"] = criterion(measured <= 1000 and tail is not None and tail <= 1000 if measured is not None else None,
                {"threshold_ms": 1000, "quiet_interval_ms": 300, "last_old_pcm_after_onset_ms": tail,
                 "first_300ms_quiet_after_onset_ms": measured, "limit": "Engineering screen on heuristically attributed client receipt, not physical audible timing."})
        else:
            checks["no_non_speech_finalized_intent"] = criterion(not server_finals if onset_unix is not None and server_rows else None,
                "All server finals after mapped input onset count, including events missing trusted promotion. No semantic text filtering; missing clock mapping is unmeasured.")
    result["speech_review"]["declared_agent_text"] = types({"agent_say"})
    result["speech_review"]["unnecessary_clarification"] = {"status": "pending_manual_review",
        "required_after_complete_request": case["expected"].get("clarifications_after_complete_request", case["expected"].get("clarifications_after_explicit_clarifier")),
        "limit": "Questions can have response/result/clarification kind. Do not infer semantic question content from kind or punctuation alone."}
    result["evidence_issues"] = evidence.issues[issue_start:]
    result["status"] = "extracted_with_evidence_gaps" if result["evidence_issues"] else "extracted"
    return result


def build_audit(protocol_path, root=ROOT):
    evidence = Evidence(root)
    protocol = evidence.load(protocol_path)
    if protocol is None:
        raise ValueError("Missing or invalid declared protocol")
    cases = {case["id"]: case for case in protocol["cases"]}
    assert len(cases) == len(protocol["cases"]), "Duplicate declared case IDs"
    output = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "Model-free extraction and mechanical case checks; semantic final speech/clarification review remains pending.",
              "arms": [], "limits": ["All declared and extra runs remain visible; no success-only timing denominator.",
                "Received PCM and event ordering are client observations, not physical speaker timing.",
                "A native empty sender stays untrusted even if its decoded payload matches a server event.",
                "ASR is transcribed content, not an automatic factual or human-listening pass.",
                "Segment counts need not equal source WAV inputs; all finals, partials and merges remain.",
                "Native STT timings and injected delivery holds remain separate; no memory/cache causal claim."]}
    evidence.reference(Path(__file__))
    asset_manifest = evidence.load(protocol_path.parent / "input-assets.json", required=False)
    assets = (asset_manifest or {}).get("assets", {})
    output["input_asset_manifest"] = asset_manifest
    for arm_declared in protocol["arms"]:
        arm = root / "results" / f"iteration20-{arm_declared['id']}"
        suite = evidence.load(arm / "run-report.json")
        if suite and suite.get("status") in {"starting", "running"}:
            raise RuntimeError(f"Wait for terminal arm before auditing: {arm}")
        stack = arm / "local-stack-run1"
        shared = evidence.rows(stack / "tool-calls.jsonl")
        stt = evidence.load(stack / "stt-diagnostics/report.json", required=False)
        delivery = evidence.load(stack / "recognition-delivery-diagnostics.json", required=False)
        asr = evidence.load(arm / "received-audio-review/report.json", required=False)
        observed = (suite or {}).get("runs", [])
        declared_names = [f"rtc-{name}-1" for name in arm_declared["cases"]]
        arm_output = {"arm_id": arm_declared["id"], "declaration": arm_declared, "run_report": suite,
            "extra_run_entries": [r for r in observed if r.get("name") not in declared_names], "cases": [],
            "stt_observer_health": {k: (stt or {}).get(k) for k in ("dropped_calls", "dropped_requests", "observation_errors", "pending_native_call_ids", "source_unchanged")},
            "unbound_stt_calls": [r for r in (stt or {}).get("calls", []) if not r.get("room")],
            "delivery_diagnostics_source": evidence.name(stack / "recognition-delivery-diagnostics.json") if delivery else None,
            "delivery_binding_errors": (delivery or {}).get("binding_errors")}
        for case_id in arm_declared["cases"]:
            row = {"case_id": case_id, "trial": f"rtc-{case_id}-1", "declaration": cases.get(case_id),
                   "criteria": {}, "speech_review": {"status": "pending_manual_review"}}
            try:
                audit_case(root, arm, cases[case_id], suite, shared, stt, delivery, asr, evidence, assets, row)
            except Exception as error:
                row.update(status="extraction_error", error=repr(error))
                evidence.issues.append({"arm": arm_declared["id"], "case_id": case_id, "error": repr(error)})
            arm_output["cases"].append(row)
        output["arms"].append(arm_output)
    all_cases = [case for arm in output["arms"] for case in arm["cases"]]
    check_counts = defaultdict(Counter)
    for case in all_cases:
        for key, check in {**case.get("criteria", {}), **case.get("effect_criteria", {})}.items():
            check_counts[key][check["status"]] += 1
    output["summary"] = {"declared_captures": len(all_cases), "case_status_counts": dict(Counter(c["status"] for c in all_cases)),
        "declared_expected_mutations": sum(len(c["declaration"]["expected"]["effects"]) for c in all_cases),
        "observed_successful_mutations": sum(len(c.get("execution", {}).get("successful_mutations", [])) for c in all_cases),
        "physical_attempts": sum(len(c.get("execution", {}).get("physical_calls", [])) for c in all_cases),
        "speech_reviews_pending": len(all_cases), "cases_without_criteria": sum(not c.get("criteria") for c in all_cases),
        "criteria_counts": {k: {**dict(v), "evaluated_case_rows": sum(v.values())} for k, v in check_counts.items()},
        "denominator_limit": "Per-criterion counts include applicable extracted rows only; unrun/error cases remain in declared_captures and status counts, never implied passes."}
    output["evidence_issues"] = evidence.issues
    output["evidence_sha256"] = evidence.hashes
    output["status"] = "extracted_with_missing_or_invalid_evidence" if evidence.issues else "extracted_manual_speech_review_pending"
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=BASE / "protocol.json")
    parser.add_argument("--out", type=Path, default=BASE / "acoustic-audit.json")
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    result = build_audit(args.protocol)
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"status": result["status"], "summary": result["summary"]}, indent=2))
    return int(any(case["status"] == "extraction_error" for arm in result["arms"] for case in arm["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
