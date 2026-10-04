"""Audit parsed semantic-plan receipts and dispatch ordering, never network counts."""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def load(path):
    return json.loads(path.read_text())


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def audit_intents(events, declared_turn_count):
    indexed = [{"server_line": number, "event": event} for number, event in enumerate(events, 1)]
    def select(kind, version):
        return [row for row in indexed if row["event"]["type"] == kind
                and row["event"]["data"].get("intent_version") == version]
    versions = set(range(1, declared_turn_count + 1))
    for row in events:
        if row["type"] in {"user_final", "plan_rejected", "plan_ready"}:
            version = row["data"].get("intent_version")
            if isinstance(version, int):
                versions.add(version)
    outcomes = []
    for version in sorted(versions):
        rejects, ready = select("plan_rejected", version), select("plan_ready", version)
        errors = [row for row in select("tool_error", version) if row["event"]["data"].get("tool") == "resolver"]
        # Production emits its deterministic no-call fallback after resolver error.
        fallback = [row for row in ready if any(error["server_line"] < row["server_line"] for error in errors)]
        accepted = [row for row in ready if row not in fallback]
        commits = select("gate_committed", version)
        dispatched = [row for row in indexed if row["event"]["type"] in {"tool_started", "tool_reused"}
                      and row["event"]["data"].get("operation_id") == f"turn-{version}"]
        final_rows = select("user_final", version)
        observed_from = min([row["server_line"] for row in final_rows + rejects + ready], default=0)
        superseding = [row for row in indexed if row["event"]["type"] == "user_final"
                       and isinstance(row["event"]["data"].get("intent_version"), int)
                       and row["event"]["data"]["intent_version"] > version
                       and row["server_line"] > observed_from]
        attempts = [row["event"]["data"].get("attempt") for row in rejects]
        first_accept = accepted[0] if accepted else None
        if rejects:
            if accepted:
                status = "repair_accepted"
            elif 2 in attempts:
                status = "repair_exhausted" if errors else "second_rejection_without_terminal_error"
            elif errors:
                status = "repair_failed_parser_or_other"
            elif superseding:
                status = "superseded_after_rejection"
            else:
                status = "rejected_without_terminal_outcome"
        elif accepted:
            status = "initial_plan_accepted"
        elif errors:
            status = "resolver_failed_without_semantic_rejection"
        elif superseding:
            status = "superseded_before_parsed_plan"
        else:
            status = "no_parsed_plan_outcome"
        allowed_calls = {(call.get("id"), call.get("tool")) for row in accepted for call in row["event"]["data"].get("calls", [])}
        checks = {
            "observed_rejection_attempt_order_bounded": attempts in ([], [1], [1, 2]),
            "at_most_one_accepted_plan": len(accepted) <= 1,
            "no_acceptance_after_second_rejection": not (2 in attempts and accepted),
            "rejections_before_accepted_plan": not accepted or all(row["server_line"] < first_accept["server_line"] for row in rejects),
            "rejection_context_present": all(isinstance(row["event"]["data"].get("utterance"), str)
                and bool(row["event"]["data"]["utterance"].strip())
                and isinstance(row["event"]["data"].get("reason"), str) and bool(row["event"]["data"]["reason"])
                and isinstance(row["event"]["data"].get("rejected_plan"), dict) for row in rejects),
            "dispatch_only_after_accepted_plan": all(first_accept is not None and row["server_line"] > first_accept["server_line"] for row in dispatched),
            "every_dispatch_call_in_accepted_plan": all((row["event"]["data"].get("id"), row["event"]["data"].get("tool")) in allowed_calls for row in dispatched),
            "no_new_dispatch_after_supersession": not superseding or all(row["server_line"] < superseding[0]["server_line"] for row in dispatched),
            "no_acceptance_after_supersession": not superseding or all(row["server_line"] < superseding[0]["server_line"] for row in accepted),
            "no_dispatch_for_unaccepted_rejection": not rejects or bool(accepted) or not dispatched,
            "fallback_plan_has_no_calls": all(not row["event"]["data"].get("calls") for row in fallback),
            "commit_follows_ready_plan": all(any(prior["server_line"] < row["server_line"] for prior in ready) for row in commits),
        }
        result_speech = [row for row in select("agent_say", version) if row["event"]["data"].get("kind") == "result"]
        checks["no_result_speech_for_unaccepted_rejection"] = not rejects or bool(accepted) or not result_speech
        accepted_attempt = 2 if accepted and rejects else 1 if accepted else None
        outcomes.append({"intent_version": version, "declared": 1 <= version <= declared_turn_count,
            "status": status, "checks": checks, "user_final_rows": final_rows,
            "rejected_plan_rows": rejects, "accepted_plan_rows": accepted, "fallback_plan_rows": fallback,
            "resolver_error_rows": errors, "gate_commit_rows": commits, "tool_dispatch_rows": dispatched,
            "superseding_user_final_rows": superseding,
            "observed_rejected_attempt_ordinals": attempts,
            "accepted_attempt_ordinal_from_control_flow": accepted_attempt,
            "parsed_plan_receipts": len(rejects) + len(accepted),
            "repair_start_receipt_available": False,
            "actual_semantic_retry_started": None,
            "wire_model_request_count": None,
            "parser_level_json_attempt_count": None,
            "ordering_boundary": "Tool starts/reuses must follow the accepted plan for this intent and match its call IDs/tools. Rejected and accepted plans can share IDs; IDs alone do not identify dispatch provenance."})
    unmatched_errors = [row for row in indexed if row["event"]["type"] == "tool_error"
                        and row["event"]["data"].get("tool") == "resolver"
                        and not isinstance(row["event"]["data"].get("intent_version"), int)]
    return {"intents": outcomes, "unattributed_resolver_errors": unmatched_errors,
            "checks": {"all_intent_invariants": all(all(row["checks"].values()) for row in outcomes),
                       "all_resolver_errors_attributed": not unmatched_errors},
            "summary": {"declared_intents": declared_turn_count, "observed_intent_rows": len(outcomes),
                "outcome_counts": dict(Counter(row["status"] for row in outcomes)),
                "rejection_events": sum(len(row["rejected_plan_rows"]) for row in outcomes),
                "accepted_plan_events": sum(len(row["accepted_plan_rows"]) for row in outcomes),
                "fallback_plan_events": sum(len(row["fallback_plan_rows"]) for row in outcomes),
                "parsed_plan_receipts": sum(row["parsed_plan_receipts"] for row in outcomes),
                "physical_tool_start_events": sum(row["event"]["type"] == "tool_started" for row in indexed),
                "tool_reuse_events": sum(row["event"]["type"] == "tool_reused" for row in indexed)}}


def main():
    target = BASE / "semantic-repair-audit.json"
    if target.exists():
        raise FileExistsError(target)
    suite, protocol = load(BASE / "run-report.json"), load(BASE / "protocol.json")
    if not suite.get("finished_at") or suite["status"] in {"starting", "running"}:
        raise RuntimeError("Wait for terminal suite including cleanup")
    trials, counts = [], Counter()
    evidence = [Path(__file__), BASE / "run-report.json", BASE / "protocol.json"]
    for case in protocol["run_order"]:
        counts[case] += 1
        name = f"rtc-{case}-{counts[case]}"
        try:
            report_path = BASE / name / "report.json"
            report = load(report_path)
            trace_path = BASE / "local-stack-run1/traces" / (report["room"] + ".jsonl")
            events = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
            data = audit_intents(events, len(protocol["expected"][case]["turns"]))
            trials.append({"name": name, "case": case, "capture_status": report["status"], **data})
            evidence.extend([report_path, trace_path])
        except Exception as error:
            trials.append({"name": name, "case": case, "audit_error": repr(error), "checks": {"raw_audit_complete": False}})
    outcomes = Counter()
    for trial in trials:
        outcomes.update(trial.get("summary", {}).get("outcome_counts", {}))
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "All declared iteration15 intents, parsed plan rejection/acceptance/fallback receipts, and actual tool dispatch order. Separate from semantic outcome and speech scoring.",
        "summary": {"declared_trials": len(trials), "audited_trials": sum("audit_error" not in row for row in trials),
            "declared_intents": sum(len(protocol["expected"][case]["turns"]) for case in protocol["run_order"]),
            "outcome_counts": dict(outcomes), **{key: sum(row.get("summary", {}).get(key, 0) for row in trials)
                for key in ("observed_intent_rows", "rejection_events", "accepted_plan_events", "fallback_plan_events", "parsed_plan_receipts", "physical_tool_start_events", "tool_reuse_events")}},
        "trials": trials,
        "failed_checks": [{"trial": trial["name"], "checks": [key for key, passed in trial["checks"].items() if not passed],
            "intents": [{"intent_version": row["intent_version"], "checks": [key for key, passed in row["checks"].items() if not passed]}
                        for row in trial.get("intents", []) if not all(row["checks"].values())]}
            for trial in trials if not all(trial["checks"].values())],
        "evidence_sha256": {str(path.relative_to(ROOT)): digest(path) for path in evidence},
        "implementation_source_sha256": {name: digest(ROOT / name) for name in (
            "agent/coordinator/coordinator.py", "agent/coordinator/resolver.py", "agent/coordinator/events.py")},
        "limits": ["A rejection event records parsed schema-valid JSON rejected by the semantic contract, not a raw network response or proof that the retry started.",
            "Accepted attempt ordinal is inferred from the frozen coordinator's one-repair control flow. No repair-start receipt exists; rejection followed by supersession does not distinguish no retry from cancellation during retry.",
            "Parser retries and transport requests are unmeasured here. Parsed receipts, fallback events, physical tool attempts and cache reuses are reported separately.",
            "plan_ready following an attributed resolver error is the deterministic fallback, not a successfully parsed model plan.",
            "Supersession is an observed later finalized intent, not proof of an internal cancellation boundary. Missing terminal outcomes remain explicit.",
            "Accepted plans and tool dispatch can still fail the frozen semantic, exact transcript or speech criteria in the separate core/received-audio audits."]}
    with target.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
