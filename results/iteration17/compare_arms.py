"""Posthoc fixed-order cache screen. Run only after all timed arms and reviews.

Stdlib only; no inference, services, model reads or mutation of source evidence.
Missing observations remain explicit. Existing nonterminal run reports block use.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
import re

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
PROTOCOL_SHA256 = "3b11cbce838a2fdea30148ce5b05f2f4e59ec5216586fdd10f844facb1b8ef87"
BOUNDARIES = {
    "api_to_submit_ms": ("engine_api_entry", "submitted"),
    "queue_ms": ("submitted", "worker_entry"),
    "native_ms": ("worker_entry", "native_completion"),
    "delivery_ms": ("native_completion", "caller_return"),
    "api_to_caller_ms": ("engine_api_entry", "caller_return"),
}
LATENCY_FIELDS = ["transcript_ms", "ack_text_ms", "result_text_ms", "first_tool_intention_ms",
                  "mutation_intention_ms", "mutation_success_ms", "first_ack_pcm_ms",
                  "first_result_pcm_ms", "result_handle_finished_ms"]
STAGE_NAMES = ["engine_load_check", "tempfile_create", "wave_open", "wave_writeframes", "wave_close",
               "model_transcribe", "parakeet_load_audio", "parakeet_get_logmel", "parakeet_generate", "tempfile_unlink"]


def metric(values):
    present = sorted(v for v in values if v is not None)
    return {"declared": len(values), "n": len(present), "missing": len(values) - len(present),
            "values": values, "p50": present[ceil(len(present) * .5) - 1] if present else None,
            "p95": present[ceil(len(present) * .95) - 1] if present else None}


def levels(values):
    present = [v for v in values if v is not None]
    return {"samples": len(values), "observed": len(present),
            "start": present[0] if present else None, "end": present[-1] if present else None,
            "min": min(present, default=None), "max": max(present, default=None),
            "end_minus_start": present[-1] - present[0] if present else None}


def memory_summary(rows):
    roles = sorted({role for r in rows for role in r.get("owned_processes", {})})
    totals, by_role, errors = [], defaultdict(list), []
    for index, row in enumerate(rows):
        observed = {}
        for role in roles:
            processes = row.get("owned_processes", {}).get(role, [])
            if isinstance(processes, dict):
                processes = [processes]
            valid = [p for p in processes if isinstance(p.get("rss"), (int, float))]
            by_role[role].append(sum(p["rss"] for p in valid) if valid else None)
            observed.update({p["pid"]: p["rss"] for p in valid})
            errors.extend({"sample": index + 1, "role": role, "row": p}
                          for p in processes if "error" in p)
        totals.append(sum(observed.values()) if observed else None)
    return {"samples": len(rows),
            "available_bytes": levels([r.get("system_memory", {}).get("available") for r in rows]),
            "swap_used_bytes": levels([r.get("swap", {}).get("used") for r in rows]),
            "observed_owned_rss_sum_bytes": levels(totals),
            "rss_by_role_bytes": {role: levels(values) for role, values in by_role.items()},
            "sampling_errors": errors,
            "scope": "Sampled whole-system levels and observed owned PIDs, excluding recorder clients. Missing processes are not zero; observed RSS sums may be partial or double-count shared pages. No allocation or paging-cause inference."}


def llm_logs(text):
    """Keep request-order log observations; do not infer unlogged token work."""
    requests, cache, orphan = [], [], []
    current = None
    for line, raw in enumerate(text.splitlines(), 1):
        if '"POST /v1/chat/completions HTTP/' in raw:
            current = {"ordinal": len(requests) + 1, "line": line, "raw": raw, "progress": []}
            requests.append(current)
        match = re.search(r"Prompt Cache: (\d+) sequences, ([\d.]+) GB", raw)
        if match:
            cache.append({"line": line, "raw": raw, "entries": int(match[1]),
                          "logical_decimal_gb": float(match[2]),
                          "request_ordinal": current["ordinal"] if current else None})
        match = re.search(r"Prompt processing progress: (\d+)/(\d+)", raw)
        if match:
            row = {"line": line, "raw": raw, "processed": int(match[1]), "total": int(match[2])}
            (current["progress"] if current else orphan).append(row)
    for row in requests:
        progress = row["progress"]
        totals = {p["total"] for p in progress}
        row["reported_remaining_prompt_tokens"] = next(iter(totals)) if len(totals) == 1 else None
        row["prefill_progress_complete"] = bool(progress) and progress[-1]["processed"] == progress[-1]["total"]
        stamps = []
        for p in progress:
            try:
                stamps.append(datetime.strptime(p["raw"][:23], "%Y-%m-%d %H:%M:%S,%f"))
            except ValueError:
                pass
        row["first_to_last_progress_log_ms"] = (stamps[-1] - stamps[0]).total_seconds() * 1000 if len(stamps) >= 2 else None
    tokens = [r["reported_remaining_prompt_tokens"] for r in requests]
    return {"request_log_rows": requests, "cache_rows": cache, "orphan_progress_rows": orphan,
            "observed_http_completion_requests": len(requests),
            "remaining_prompt_tokens": metric(tokens),
            "sum_reported_remaining_prompt_tokens": sum(v for v in tokens if v is not None) if any(v is not None for v in tokens) else None,
            "prefill_progress_intervals_ms": metric([r["first_to_last_progress_log_ms"] for r in requests]),
            "max_observed_cache_entries": max((r["entries"] for r in cache), default=None),
            "max_observed_logical_cache_gb": max((r["logical_decimal_gb"] for r in cache), default=None),
            "limits": "Logs are observations, not a full prompt/response capture. Progress intervals exclude unlogged work; reported remaining tokens are not full prompt lengths. Snapshot maxima can miss retention peaks. No GPU, allocation, physical wire retry or prompt-equality claim."}


class Evidence:
    def __init__(self):
        self.hashes, self.issues = {}, []

    def bytes(self, path):
        try:
            data = path.read_bytes()
        except OSError as error:
            self.issues.append({"path": str(path), "error": repr(error)})
            return None
        self.hashes[str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)] = sha256(data).hexdigest()
        return data

    def json(self, path):
        data = self.bytes(path)
        try:
            return json.loads(data) if data is not None else {}
        except (ValueError, UnicodeError) as error:
            self.issues.append({"path": str(path), "error": repr(error)})
            return {}

    def lines(self, path):
        data, rows = self.bytes(path), []
        for line, raw in enumerate(data.splitlines() if data is not None else [], 1):
            try:
                rows.append(json.loads(raw))
            except (ValueError, UnicodeError) as error:
                self.issues.append({"path": str(path), "line": line, "raw": raw.decode(errors="replace"), "error": repr(error)})
        return rows


def arm_report(arm, protocol, evidence):
    directory = ROOT / arm["directory"]
    names = [d for d in protocol["declared_trials"] if d["arm_id"] == arm["arm_id"]]
    documents = {name: evidence.json(directory / filename) for name, filename in {
        "run": "run-report.json", "protocol": "protocol.json", "audit": "audit.json",
        "latency": "latency-summary.json", "completion": "completion-latency.json",
        "speech": "received-audio-review/qualitative-review.json", "semantic": "semantic-repair-audit.json",
        "supplement": "supplementary-log-review.json", "stt_audit": "stt-stage-independent-audit.json",
        "stt_raw": "local-stack-run1/stt-diagnostics/report.json", "launch": "local-stack-run1/llm-launch.json",
        "cleanup": "pre-asr-cleanup.json", "preflight": "preflight.json"}.items()}
    suite = documents["run"]
    if suite and (not suite.get("finished_at") or suite.get("status") == "running"):
        raise RuntimeError(f"Refuse to analyze nonterminal arm: {arm['arm_id']}")
    memory = evidence.lines(directory / "memory.jsonl")
    logs = llm_logs((evidence.bytes(directory / "local-stack-run1/llm.log") or b"").decode(errors="replace"))
    audits = {r["name"]: r for r in documents["audit"].get("trials", [])}
    speeches = {r["id"]: r for r in documents["speech"].get("trials", [])}
    completions = {r["trial"]: r for r in documents["completion"].get("per_case", [])}
    latency_rows = documents["latency"].get("per_turn", [])
    roles = defaultdict(list)
    for mapping in documents["stt_audit"].get("capture_mapping", []):
        for turn in mapping.get("turns", []):
            if turn.get("call_id"):
                roles[turn["call_id"]].append({"trial": mapping["trial"], "input_label": turn.get("input_label")})
    native = []
    for call in documents["stt_raw"].get("calls", []):
        timings = {key: (call[end]["monotonic_ns"] - call[start]["monotonic_ns"]) / 1e6
                   if start in call and end in call else None for key, (start, end) in BOUNDARIES.items()}
        stages = [{"name": s["name"], "status": s.get("status"),
                   "milliseconds": (s["end"]["monotonic_ns"] - s["start"]["monotonic_ns"]) / 1e6 if "end" in s else None}
                  for s in call.get("stages", [])]
        timings.update({s["name"] + "_ms": s["milliseconds"] for s in stages})
        pcm = {}
        for label in ("submitted_pcm", "written_wav_pcm"):
            receipt = call.get(label, {})
            if receipt.get("file"):
                data = evidence.bytes(directory / "local-stack-run1/stt-diagnostics" / receipt["file"])
                pcm[label] = {**receipt, "verified": data is not None and len(data) == receipt.get("bytes") and sha256(data).hexdigest() == receipt.get("sha256")}
                if not pcm[label]["verified"]:
                    evidence.issues.append({"arm": arm["arm_id"], "call": call["call_id"], "error": f"{label} identity mismatch"})
        start, end = call.get("engine_api_entry", {}).get("monotonic_ns"), call.get("caller_return", {}).get("monotonic_ns")
        window = [r for r in memory if start is not None and end is not None and start / 1e9 <= r.get("monotonic", -1) <= end / 1e9]
        native.append({"arm_id": arm["arm_id"], "call_id": call["call_id"], "call_type": call["call_type"],
                       "roles": roles[call["call_id"]], "timings_ms": timings, "stages": stages, "pcm": pcm,
                       "caller": call.get("caller_return"), "native": call.get("native_completion"), "future": call.get("native_future_done"),
                       "memory_window_monotonic_ns": {"api_entry": start, "caller_return": end},
                       "model_reference": {k: call.get(k) for k in ("model_kind", "model_name", "revision")},
                       "memory_during_api_window": memory_summary(window), "window_sample_monotonic": [r["monotonic"] for r in window]})
    cases, turns = [], []
    for declared in names:
        trial = Path(declared["directory"]).name
        core, spoken, completed = audits.get(trial, {}), speeches.get(trial, {}), completions.get(trial, {})
        semantic = bool(core.get("semantic_checks")) and all(v is True for v in core["semantic_checks"].values())
        speech_ok = spoken.get("manual_review", {}).get("complete_declared_final_speech") is True
        source = evidence.json(ROOT / declared["directory"] / "report.json")
        case_turns = documents["protocol"].get("expected", {}).get(declared["case"], {}).get("turns", [])
        row = {**declared, "trial": trial, "capture_status": source.get("status", "missing_or_unrun"),
               "capture_failure": {k: source.get(k) for k in ("failed_stage", "error", "cleanup_errors")},
               "backend_case_passed": semantic, "final_speech_passed": speech_ok,
               "semantic_checks": core.get("semantic_checks", {}), "all_checks": core.get("checks", {}),
               "speech_review": spoken, "physical_calls": core.get("calls", []), "effects": core.get("effects", []),
               "reuse": core.get("reused_rows", []), "interruptions": core.get("interruptions", []),
               "observed_finished_reply_ms": completed.get("observed_finished_reply_ms"),
               "fulfilled_reply_ms": completed.get("fulfilled_case_reply_finish_ms") if semantic and speech_ok and completed.get("result_handle_finished") is True else None}
        if row["fulfilled_reply_ms"] != completed.get("fulfilled_case_reply_finish_ms"):
            evidence.issues.append({"arm": arm["arm_id"], "trial": trial, "error": "Completion gate mismatch"})
        cases.append(row)
        for version in range(1, declared["expected_turns"] + 1):
            label = "initial" if version == 1 else f"followup_{version-1}"
            timed = [r for r in latency_rows if r["trial"] == trial and r["intent_version"] == version]
            calls = [c for c in native if c["call_type"] == "_recognize_impl" and {"trial": trial, "input_label": label} in c["roles"]]
            turns.append({"arm_id": arm["arm_id"], "trial": trial, "case": declared["case"], "intent_version": version,
                          "input_label": label, "declared_text": case_turns[version-1].get("text") if version <= len(case_turns) else None,
                          "latency_observations": timed, "stt_call_ids": [c["call_id"] for c in calls],
                          "stt_ms": calls[0]["timings_ms"] if len(calls) == 1 and len(calls[0]["roles"]) == 1 else {},
                          "missing_or_ambiguous_stt": len(calls) != 1 or len(calls[0]["roles"]) != 1})
    groups = {"all": cases, "cancel": [r for r in cases if r["case"] == "cancel"], "repeat": [r for r in cases if r["case"] == "repeat"]}
    timing_names = sorted(set(LATENCY_FIELDS) | {k for r in turns for t in r["latency_observations"] for k in t.get("speech_end_to_client_observation_ms", {})})
    stt_names = sorted(set(BOUNDARIES) | {s + "_ms" for s in STAGE_NAMES} | {k for c in native for k in c["timings_ms"]})
    turn_groups = {"all": turns, "initial": [r for r in turns if r["intent_version"] == 1], "followup": [r for r in turns if r["intent_version"] > 1]}
    latency_groups = {name: {key: metric([r["latency_observations"][0].get("speech_end_to_client_observation_ms", {}).get(key) if len(r["latency_observations"]) == 1 else None for r in rows]) for key in timing_names} for name, rows in turn_groups.items()}
    checks = [r["all_checks"] for r in cases]
    required = ["all_final_transcripts_exact", "effect_count"]
    quality = (all(r["backend_case_passed"] and r["final_speech_passed"] and r["capture_status"] == "completed_observation"
                   and r["fulfilled_reply_ms"] is not None for r in cases)
               and all(all(c.get(k) is True for k in required) for c in checks)
               and sum(c.get(k) is True for c in checks for k in c if k.startswith("old_result_interrupted_")) == arm["expected"]["interruption_pairs"])
    command = documents["launch"].get("command", [])
    try:
        at = command.index("--prompt-cache-size")
        factor_ok = command[at + 1] == str(arm["cache_entries"])
        normalized_command = command[:at+1] + ["<controlled-entry-count>"] + command[at+2:]
    except (ValueError, IndexError):
        factor_ok, normalized_command = False, None
    absences = documents["cleanup"].get("observations", [])
    clean = bool(absences) and all(r.get("absent") is True for r in absences) and suite.get("worker_clean_exit") is True and suite.get("all_agents_departed") is True
    preflight = documents["preflight"]
    shared_sources = {p: h for p, h in preflight.get("source_input_method_sha256", {}).items()
                      if Path(p).is_relative_to(ROOT / "agent") or p in {
                          str(ROOT / "scripts/local_livekit_audio_experiment.py"), str(ROOT / "scripts/local_livekit_worker.py")}}
    observer_sources = {("arm/" + str(Path(p).relative_to(directory)) if Path(p).is_relative_to(directory)
                         else str(Path(p).relative_to(ROOT))): h for p, h in preflight.get("source_input_method_sha256", {}).items()
                        if Path(p).name in {"stt_diagnostics.py", "instrumented_worker.py"}}
    model_receipts = preflight.get("model_files", {})
    return {"arm_id": arm["arm_id"], "cache_entries": arm["cache_entries"], "cases": cases, "turns": turns,
            "quality_gate": quality, "factor_setting_matches": factor_ok, "normalized_llm_command": normalized_command,
            "frozen_sources_match": suite.get("frozen_inputs_unchanged") is True and not documents["audit"].get("current_frozen_input_mismatches", ["missing"]),
            "cleanup_gate": clean, "run_status": suite.get("status", "missing_or_unrun"),
            "shared_source_receipts": shared_sources, "observer_source_receipts": observer_sources,
            "preflight_model_receipts": model_receipts,
            "preflight_models_match": bool(model_receipts) and all(r.get("reference") == r.get("observed") for r in model_receipts.values()),
            "completion_groups_ms": {name: metric([r["fulfilled_reply_ms"] for r in rows]) for name, rows in groups.items()},
            "latency_groups_ms": latency_groups, "reported_phase_metrics": documents["latency"].get("phases"),
            "reported_interruption_metrics": documents["latency"].get("interruption_metrics"),
            "stt_declared_turn_metrics_ms": {k: metric([r["stt_ms"].get(k) for r in turns]) for k in stt_names},
            "native_calls": native, "outer_stt_requests": documents["stt_raw"].get("requests", []),
            "native_call_type_counts": dict(Counter(c["call_type"] for c in native)),
            "native_outcome_counts": dict(Counter((c["native"] or {}).get("status", "pending") for c in native)),
            "unmapped_native_calls": [c["call_id"] for c in native if c["call_type"] != "warmup" and not c["roles"]],
            "stt_audit_health": documents["stt_audit"].get("diagnostic_health"),
            "stt_audit_consistent": documents["stt_audit"].get("artifact_consistency_checks_pass"),
            "llm": logs, "memory": memory_summary(memory), "run_receipt": suite,
            "audit_summary": documents["audit"].get("summary"), "failed_checks": documents["audit"].get("failed_checks"),
            "supplementary": documents["supplement"], "semantic_repair": documents["semantic"],
            "undeclared_observations": {"core_trials": [r for n, r in audits.items() if n not in {c["trial"] for c in cases}],
                                        "latency_rows": [r for r in latency_rows if not any(t["trial"] == r["trial"] and t["intent_version"] == r["intent_version"] for t in turns)]}}


def main():
    output = BASE / "comparison-report.json"
    if output.exists():
        raise FileExistsError("Preserve existing comparison report")
    evidence = Evidence()
    protocol_path = BASE / "comparison-protocol.json"
    protocol = evidence.json(protocol_path)
    if evidence.hashes.get(str(protocol_path.relative_to(ROOT))) != PROTOCOL_SHA256:
        raise ValueError("Comparison protocol differs from the declared frozen version")
    arms = [arm_report(arm, protocol, evidence) for arm in protocol["arms"]]
    evidence.bytes(Path(__file__))
    groups, unmatched = defaultdict(list), []
    for arm in arms:
        for call in arm["native_calls"]:
            if call["call_type"] == "warmup":
                continue
            pcm = call["pcm"].get("submitted_pcm", {})
            if len(call["roles"]) == 1 and pcm.get("verified"):
                role = call["roles"][0]
                key = (role["trial"], role["input_label"], pcm["sha256"], pcm["bytes"], pcm["sample_rate"])
                groups[key].append(call)
            else:
                unmatched.append({"reason": "role ambiguous/missing or PCM unverified", "call": call})
    matches = []
    for key, calls in groups.items():
        if len({c["arm_id"] for c in calls}) > 1:
            matches.append({"role_trial": key[0], "input_label": key[1], "sha256": key[2], "bytes": key[3], "sample_rate": key[4], "calls": calls})
        else:
            unmatched.extend({"reason": "no cross-arm exact role/PCM match", "call": c} for c in calls)
    a, b, c = arms
    commands_match = a["normalized_llm_command"] is not None and a["normalized_llm_command"] == b["normalized_llm_command"] == c["normalized_llm_command"]
    shared_match = all(bool(a[key]) and a[key] == b[key] == c[key]
                       for key in ("shared_source_receipts", "observer_source_receipts", "preflight_model_receipts"))
    complete = all(arm["completion_groups_ms"]["all"]["n"] == 4 for arm in arms)
    invalid = bool(evidence.issues) or not commands_match or not shared_match or not all(r["factor_setting_matches"] and r["frozen_sources_match"] and r["cleanup_gate"] and r["stt_audit_consistent"] and r["preflight_models_match"] for r in arms)
    peaks = [r["llm"]["max_observed_logical_cache_gb"] for r in arms]
    less_cache = all(v is not None for v in peaks) and peaks[1] < peaks[0] and peaks[1] < peaks[2]
    no_slower = complete and all(b["completion_groups_ms"]["all"][p] <= baseline["completion_groups_ms"]["all"][p] for baseline in (a, c) for p in ("p50", "p95"))
    one_entry = b["llm"]["max_observed_cache_entries"] == 1
    if invalid:
        decision = "invalid_or_inconclusive"
    elif not b["quality_gate"]:
        decision = "candidate_quality_failure"
    elif less_cache and no_slower and one_entry:
        decision = "promising_for_replication_only"
    else:
        decision = "observed_tradeoff_or_inconclusive_retain_two_entries"
    deltas = {pair: {p: right["completion_groups_ms"]["all"][p] - left["completion_groups_ms"]["all"][p]
                    if right["completion_groups_ms"]["all"][p] is not None and left["completion_groups_ms"]["all"][p] is not None else None for p in ("p50", "p95")}
              for pair, left, right in (("B_minus_A_first", a, b), ("B_minus_A_return", c, b), ("A_return_minus_A_first", a, c))}
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "protocol_sha256": PROTOCOL_SHA256,
              "scope": "Posthoc derivation; three fresh stacks in fixed A/B/A order, one B process. No inference or causal allocation/latency claim.",
              "declared_denominators": protocol["full_denominators"], "arms": arms,
              "retained_case_rows": sum(len(r["cases"]) for r in arms), "retained_turn_rows": sum(len(r["turns"]) for r in arms),
              "exact_role_pcm_matches": matches, "unmatched_recognition_calls": unmatched,
              "exact_pcm_subset_counts": {"groups": len(matches), "matched_calls": sum(len(g["calls"]) for g in matches), "unmatched_calls": len(unmatched)},
              "completion_percentile_differences_ms": deltas,
              "screen_decision": {"classification": decision, "commands_equal_except_control": commands_match,
                                  "shared_source_observer_model_receipts_equal": shared_match,
                                  "all_arm_completion_denominators_measured": complete, "B_quality_gate": b["quality_gate"],
                                  "B_observed_one_entry": one_entry, "B_lower_logical_cache_peak_than_each_A": less_cache,
                                  "B_completion_percentiles_no_worse_than_each_A": no_slower,
                                  "production_change_adopted": False},
              "evidence_issues": evidence.issues, "evidence_sha256": evidence.hashes,
              "limits": protocol["limits"] + [
                  "This method is postfreeze analysis and was not instrumentation used in the timed arms. It preserves all declared cases/turns and additional observed work.",
                  "Exact-PCM subsets do not replace full denominators; repeated calls/process-shared observations are dependent. Warmups remain separately retained.",
                  "LLM progress-log intervals are not complete prefill service times. Adaptive prompt equality and unlogged token/decode metrics remain unavailable.",
                  "STT memory windows use same-host monotonic clocks. LLM logs lack monotonic request boundaries, so no invented cross-clock memory/request alignment is made.",
                  "Known trace attribution and SDK job failures remain in every arm's full audits. They are not repaired by content/effect success or excluded from reporting.",
                  "A favorable screen means replication is warranted, not default adoption, independent acoustic accuracy, memory allocation savings or causal superiority."]}
    with output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"output": str(output), "decision": result["screen_decision"], "issues": evidence.issues}, indent=2))


if __name__ == "__main__":
    main()
