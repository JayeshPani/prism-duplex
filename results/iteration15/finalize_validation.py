"""Verify the semantic-repair checkpoint without hiding the remaining speech failure."""
import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
import subprocess

import psutil

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def read(name):
    return json.loads((BASE / name).read_text())


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def verify(mapping, root=ROOT):
    mismatches = [name for name, value in mapping.items() if digest(root / name) != value]
    assert not mismatches, mismatches
    return len(mapping)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def verify_completion(protocol, audit, speech, latency, completion):
    counts, names = {}, []
    for case in protocol["run_order"]:
        counts[case] = counts.get(case, 0) + 1
        names.append(f"rtc-{case}-{counts[case]}")
    assert [row["trial"] for row in completion["per_case"]] == names
    assert [row["id"] for row in speech["trials"]] == names
    assert len(latency["per_turn"]) == sum(len(protocol["expected"][case]["turns"]) for case in protocol["run_order"])
    for row, case in zip(completion["per_case"], protocol["run_order"]):
        version = len(protocol["expected"][case]["turns"])
        core = next(t for t in audit["trials"] if t["name"] == row["trial"])
        spoken = next(t for t in speech["trials"] if t["id"] == row["trial"])
        timed = [t for t in latency["per_turn"] if t["trial"] == row["trial"] and t["intent_version"] == version]
        assert len(timed) == 1
        timed = timed[0]
        semantic = bool(core["semantic_checks"]) and all(core["semantic_checks"].values())
        facts = spoken["manual_review"]["complete_declared_final_speech"]
        finished = timed["result_handle_state"] == "finished"
        observed = timed["speech_end_to_client_observation_ms"]["result_handle_finished_ms"]
        assert row["case"] == case and row["final_intent_version"] == version
        assert row["semantic_case_passed"] is semantic is spoken["semantic_effect_case_passed"]
        assert row["all_declared_final_speech_facts"] is facts
        assert row["result_handle_finished"] is finished
        assert row["observed_finished_reply_ms"] == observed
        assert row["fulfilled_case_reply_finish_ms"] == (observed if semantic and facts and finished else None)
    assert set(completion["groups"]) == {"all", *protocol["expected"]}
    for name, group in completion["groups"].items():
        values = [r["fulfilled_case_reply_finish_ms"] for r in completion["per_case"] if name == "all" or r["case"] == name]
        present = sorted(v for v in values if v is not None)
        assert group["values_ms_in_run_order"] == values
        assert group["declared"] == len(values) and group["n"] == len(present)
        assert group["failed_or_unmeasured"] == len(values) - len(present)
        for percentile in (50, 95):
            expected = present[ceil(percentile / 100 * len(present)) - 1] if present else None
            assert group[f"p{percentile}_ms"] == expected


def main():
    target = BASE / "final-validation.json"
    assert not target.exists(), "Preserve the existing checkpoint."
    tested = read("tested-source.json")
    assert tested["exit_code"] == 0 and tested["unchanged_during_test"]
    assert tested["paths_sha256"] == tested["after_sha256"]
    test_count = verify(tested["paths_sha256"])
    assert tested["tests"].startswith("617 passed"), tested["tests"]
    assert digest(BASE / "full-tests.txt") == tested["log_sha256"]
    protocol, preflight, suite = read("protocol.json"), read("preflight.json"), read("run-report.json")
    verify(preflight["source_input_method_sha256"])
    assert suite["status"] == "completed" and suite["finished_at"] and suite["frozen_inputs_unchanged"]
    frozen_count = verify(suite["frozen_sha256"])
    assert frozen_count == 39, frozen_count
    post = read("post-inference-validation.json")
    verify(post["evidence_sha256"])
    assert len(post["model_files"]) == 10 and set(post["model_files"]) == set(preflight["model_files"])
    assert all(row["matches"] and row["stable_stat_during_read"] and
               row["expected"] == row["observed"] == preflight["model_files"][name]["reference"]
               for name, row in post["model_files"].items())
    for inventory in ("frozen_files", "frozen_files_rechecked_after_models"):
        assert set(post[inventory]) == set(suite["frozen_sha256"])
        assert all(row["matches"] and row["expected"] == row["observed"] == suite["frozen_sha256"][name]
                   for name, row in post[inventory].items())
    audit, semantic, supplement = (read(name) for name in ("audit.json", "semantic-repair-audit.json", "supplementary-log-review.json"))
    for item in (audit, semantic, supplement):
        verify(item["evidence_sha256"])
        assert item["failed_checks"] == []
    verify(semantic["implementation_source_sha256"])
    assert all(audit["suite_checks"].values()) and not audit["current_frozen_input_mismatches"]
    assert audit["summary"]["observed_effects"] == audit["summary"]["expected_effects"] == 10
    assert semantic["summary"]["declared_intents"] == 10
    assert semantic["summary"]["outcome_counts"] == {"initial_plan_accepted": 9, "repair_accepted": 1}
    assert semantic["summary"]["physical_tool_start_events"] == audit["summary"]["shared_attempts"] == 23
    stage = read("stt-stage-independent-audit.json")
    assert stage["artifact_consistency_checks_pass"]
    verify(stage["source_sha256"], BASE)
    verify(stage["verified_pcm_sha256"], BASE)
    speech, asr = read("received-audio-review/qualitative-review.json"), read("received-audio-review/report.json")
    verify(speech["input_sha256"], BASE)
    verify(speech["raw_and_resampled_sha256"], BASE)
    verify(asr["source_sha256"])
    assert asr["status"] == "completed" and asr["inputs_unchanged"] and asr["source_unchanged"] and asr["suite_unchanged"]
    assert digest(BASE / "received-audio-review/protocol.json") == asr["protocol_sha256"]
    assert len(asr["trials"]) == 4 and all(row["status"] == "completed" for row in asr["trials"])
    speech_failures = [{"trial": row["id"], "facts": row["manual_review"]["facts"],
                       "note": row["manual_review"]["note"], "semantic_effect_case_passed": row["semantic_effect_case_passed"]}
                      for row in speech["trials"] if not row["manual_review"]["complete_declared_final_speech"]]
    assert [row["trial"] for row in speech_failures] == ["rtc-repeat-2"]
    assert speech_failures[0]["facts"] == {"airport_navigation_active": True, "office_replaced": False, "arrival_88_minutes": True}
    assert speech["counts"]["complete_declared_final_speech_facts"] == 3 and speech["counts"]["declared"] == 4
    latency, completion = read("latency-summary.json"), read("completion-latency.json")
    verify(latency["source_sha256"], BASE)
    verify(completion["source_sha256"], BASE)
    verify_completion(protocol, audit, speech, latency, completion)
    code_review, isolation = read("independent-code-review.json"), read("change-isolation.json")
    verify(code_review["reviewed_source_sha256"])
    check = code_review["independent_execution"]
    assert check["exit_code"] == 0 and digest(ROOT / check["stored_exact_python_body"]) == check["stored_exact_python_body_sha256"]
    baseline = {row["original"]: row for row in json.loads((ROOT / isolation["baseline"]).read_text())["files"]}
    for row in isolation["observations"]:
        assert digest(ROOT / row["path"]) == row["after_sha256"]
        assert digest(ROOT / baseline[row["path"]]["snapshot"]) == row["before_sha256"]
        assert row["changed"] == (row["before_sha256"] != row["after_sha256"])
    assert isolation["changed_production_files"] == [row["path"] for row in isolation["observations"] if row["changed"]]
    snapshots = read("source-snapshot.json")
    assert len(snapshots["files"]) == 94
    for row in snapshots["files"]:
        assert digest(ROOT / row["snapshot"]) == row["sha256"] == digest(ROOT / row["original"])
    pids = {r["pid"] for r in read("pre-asr-cleanup.json")["observations"]}
    pids.add(read("received-audio-supervision.json")["pid"])
    assert len(pids) == 8 and pids == set(post["suite_pids"]) | {post["asr_pid"]}
    assert post["all_owned_pids_absent"] and post["asr_pid_absent"]
    absent = {str(pid): not psutil.pid_exists(pid) for pid in sorted(pids)}
    assert all(absent.values()), absent
    scripts = [p for p in BASE.rglob("*.py") if not {"source-snapshot", "baseline-source"}.intersection(p.parts)]
    for path in scripts:
        ast.parse(path.read_text(), filename=str(path))
    subprocess.run(["git", "diff", "--check"], cwd=ROOT, check=True)
    files = [p for p in BASE.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    documentation = [ROOT / "README.md", ROOT / "docs/iteration15.md", ROOT.parent / "prism-pr-description.md", ROOT / "docs/completion-audit.md"]
    assert all(path.is_file() for path in documentation)
    files += documentation
    reviews = ["independent-code-review.json", "change-isolation.json", "stt-stage-independent-audit.json",
               "semantic-repair-audit.json", "semantic-repair-audit-fixtures-final.txt", "supplementary-log-review.json",
               "received-audio-review/qualitative-review.json", "completion-latency.json"]
    inherited_labels = {name: read(name)["scope"] for name in ("audit.json", "supplementary-log-review.json")
                        if "iteration14" in read(name)["scope"]}
    receipt = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "checkpoint_verified_with_known_failures", "task_complete": False,
        "scope": "Source, tests and retained evidence verified at a checkpoint. Backend and semantic-repair checks pass; one final speech omits the replaced-office fact. Capture completion is not complete objective fulfillment.",
        "tests": tested["tests"], "tested_source_files_unchanged": test_count,
        "rtc_frozen_files_unchanged": frozen_count, "source_files_snapshotted": len(snapshots["files"]),
        "experiment_python_files_parsed": len(scripts), "git_diff_check": "passed",
        "rtc_summary": audit["summary"], "failed_checks": audit["failed_checks"], "failed_core_check_count": 0,
        "semantic_repair_summary": semantic["summary"], "semantic_repair_failed_checks": semantic["failed_checks"],
        "failed_final_speech_cases": speech_failures, "completion_latency_consistent": True,
        "completion_latency_groups": completion["groups"],
        "stt_stage_counts": stage["counts"], "whole_stream_review_counts": speech["counts"],
        "supplementary_summary": supplement["summary"], "pre_and_post_model_references_match": 10,
        "model_verification_boundary": "Fresh full-byte model hashes are in post-inference-validation.json; this finalizer verifies that receipt against preflight references without loading models or rehashing weights again.",
        "owned_pids_absent": absent,
        "documentation_sha256": {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): digest(path) for path in documentation},
        "review_receipts": {name: digest(BASE / name) for name in reviews},
        "inherited_scope_label_typos": inherited_labels,
        "inherited_label_boundary": "Frozen copied core/supplement scope prose says iteration14; paths, protocol, source receipts and actual evidence are iteration15. Frozen methods and original derived outputs remain unchanged.",
        "syntax_check_boundary": "Only current experiment *.py files are parsed, excluding source-snapshot and baseline-source. Prior failed fixture receipts remain hashed without rewriting.",
        "delivery": {"branch": git("branch", "--show-current"), "head": git("rev-parse", "HEAD"),
                     "commits_ahead": int(git("rev-list", "--count", "origin/main..HEAD")),
                     "staged_files": git("diff", "--cached", "--name-only").splitlines(),
                     "status": "No commit, push or PR created by this iteration; user-selected staging required."},
        "sha256": {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p): digest(p) for p in sorted(set(files))},
        "remaining": ["Second return final speech omits the replaced-office fact despite correct backend replacement",
                      "Only one accepted live repair; no live exhausted/superseded-repair coverage or general plan-fidelity claim",
                      "Live delayed participant attribution remains unexercised",
                      "Recognition variance, human microphone/echo/physical playback and concurrent-room validation",
                      "Upstream acknowledged graceful completion and fresh adapted FDB evaluation",
                      "User-selected staging for PR publication and organizer Theme 05 brief access"],
    }
    with target.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(json.dumps({k: receipt[k] for k in ("tests", "tested_source_files_unchanged", "rtc_frozen_files_unchanged", "source_files_snapshotted", "delivery")}, indent=2))


if __name__ == "__main__":
    main()
