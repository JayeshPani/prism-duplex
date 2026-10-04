"""Verify all three cache arms without hiding interruption or attribution failures.

Execute only after docs are final. Capture stdout in memory and write it after exit.
No tests, model loads, or model-weight reads occur in this finalizer.
"""
import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil
import os
from pathlib import Path
import stat
import subprocess
import sys

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
ARMS = (("A2_first", "iteration17-cache2-before", 2),
        ("B1", "iteration17-cache1", 1),
        ("A2_return", "iteration17-cache2-after", 2))
TRIALS = ["rtc-cancel-1", "rtc-repeat-1", "rtc-repeat-2", "rtc-cancel-2"]
EXPECTED_FAILURES = {
    "A2_first": [{"name": name, "checks": ["full_trace_equality"]}
                 for name in ("rtc-repeat-1", "rtc-repeat-2")],
    "B1": [{"name": "rtc-cancel-1", "checks": ["old_result_interrupted_1_to_2", "speech_handles_settled"]},
           {"name": "rtc-repeat-1", "checks": ["full_trace_equality"]},
           {"name": "rtc-repeat-2", "checks": ["old_result_interrupted_1_to_2", "speech_handles_settled"]},
           {"name": "rtc-cancel-2", "checks": ["old_result_interrupted_1_to_2", "speech_handles_settled"]}],
    "A2_return": [{"name": "rtc-repeat-1", "checks": ["full_trace_equality"]}],
}


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    value = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def verify(mapping, root=ROOT):
    for name, expected in mapping.items():
        assert digest(root / name) == expected, name
    return len(mapping)


def verify_recorded_inventory(rows, expected):
    # Explicit phase completeness prevents a merely 'passed' partial receipt.
    assert set(rows) == set(expected), (len(rows), len(expected))
    for name, row in rows.items():
        assert row["matches"] and row["stable_stat_during_read"], name
        assert row["expected"] == row["observed"] == expected[name], name
        assert row["stat_before"] == row["stat_after"] == row["path_stat_after"], name
        assert row["bytes_read"] == row["stat_before"]["st_size"], name
        assert not row.get("error"), name


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def verify_metric(group, values, missing_key="missing_or_interrupted"):
    present = sorted(v for v in values if v is not None)
    assert group["values_ms_in_run_order"] == values
    assert group["declared"] == len(values) and group["n"] == len(present)
    assert group[missing_key] == len(values) - len(present)
    for percentile in (50, 95):
        expected = present[ceil(percentile / 100 * len(present)) - 1] if present else None
        assert group[f"p{percentile}_ms"] == expected


def verify_latency(audit, latency):
    expected = [(t["name"], row["intent_version"]) for t in audit["trials"] for row in t["turns"]]
    assert [(r["trial"], r["intent_version"]) for r in latency["per_turn"]] == expected
    assert len(expected) == 10
    for timed in latency["per_turn"]:
        trial = next(t for t in audit["trials"] if t["name"] == timed["trial"])
        turn = next(t for t in trial["turns"] if t["intent_version"] == timed["intent_version"])
        assert timed["speech_end_to_client_observation_ms"] == turn["speech_end_to_client_observation_ms"]
        if timed["result_handle_state"] == "interruption_requested":
            assert timed["speech_end_to_client_observation_ms"]["result_handle_finished_ms"] is None
    pairs = [(t["name"], pair) for t in audit["trials"] for pair in t["interruptions"]]
    assert len(pairs) == len(latency["interruptions"]) == 6
    for row, (trial, pair) in zip(latency["interruptions"], pairs):
        assert row["trial"] == trial
        for key in ("from_intent_version", "to_intent_version", "old_handle_state",
                    "last_old_pcm_after_input_onset_ms", "handle_interruption_after_input_onset_ms"):
            assert row[key] == pair[key], (trial, key)
        if row["old_handle_state"] != "interruption_requested":
            assert row["handle_interruption_after_input_onset_ms"] is None
    for key, group in latency["interruption_metrics"].items():
        verify_metric(group, [pair[key] for _, pair in pairs])


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
    assert not target.exists(), "Preserve the existing receipt."
    assert not stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode), "Capture stdout in memory; do not redirect to an open file."
    assert not (BASE / "final-validation-output.txt").exists(), "Write stdout only after exit."
    tested = read(ROOT / "results/iteration16/tested-source.json")
    assert tested["exit_code"] == 0 and tested["unchanged_during_test"]
    assert tested["paths_sha256"] == tested["after_sha256"]
    assert tested["tests"] == "655 passed in 5.42s"
    assert verify(tested["paths_sha256"]) == 76
    assert digest(ROOT / "results/iteration16/full-tests.txt") == tested["log_sha256"]
    snapshot = read(BASE / "source-snapshot.json")
    assert len(snapshot["files"]) == 150
    originals = {str(ROOT / r["original"]): r["sha256"] for r in snapshot["files"]}
    copies = {str(ROOT / r["snapshot"]): r["sha256"] for r in snapshot["files"]}
    assert len(originals) == len(copies) == 150
    verify(originals); verify(copies)
    post = read(BASE / "post-inference-validation.json")
    assert post["status"] == "passed" and post["issues"] == [] and not post.get("fatal_error")
    assert post["model_read_started_at_utc"] and post["model_read_finished_at_utc"] and post["checked_at_utc"]
    verify(post["evidence_sha256"])
    assert digest(BASE / "verify_after_inference.py") == post["method_sha256"]
    evidence_map = {str(ROOT / name): value for name, value in post["evidence_sha256"].items()}
    verify_recorded_inventory(post["evidence_recheck"], evidence_map)
    for phase in ("before_models", "after_models"):
        verify_recorded_inventory(post[f"global_originals_{phase}"], originals)
        verify_recorded_inventory(post[f"global_snapshots_{phase}"], copies)
    assert set(post["arms"]) == {r[0] for r in ARMS}
    comparison = read(BASE / "comparison-report.json")
    declared = read(BASE / "comparison-protocol.json")["full_denominators"]
    assert comparison["declared_denominators"] == declared
    assert declared["captures"] == comparison["retained_case_rows"] == 12
    assert declared["input_turns"] == comparison["retained_turn_rows"] == 30
    assert declared["interruption_pairs"] == 18
    assert comparison["screen_decision"]["classification"] == "candidate_quality_failure"
    assert comparison["screen_decision"]["production_change_adopted"] is False
    assert comparison["screen_decision"]["B_quality_gate"] is False
    assert comparison["evidence_issues"] == []
    verify(comparison["evidence_sha256"])
    rtc_review = read(BASE / "rtc-audit-review.json")
    verify(rtc_review["evidence_sha256"])
    owners, arm_rows, model_refs = [], [], []
    for arm_id, directory, entries in ARMS:
        arm = ROOT / "results" / directory
        protocol, preflight, suite = [read(arm / name) for name in ("protocol.json", "preflight.json", "run-report.json")]
        assert preflight["status"] == "passed" and preflight["arm_id"] == suite["arm_id"] == arm_id
        assert preflight["cache_entries"] == suite["cache_entries"] == entries
        verify(preflight["source_input_method_sha256"])
        assert suite["status"] == "completed" and suite["finished_at"] and suite["frozen_inputs_unchanged"]
        assert verify(suite["frozen_sha256"]) == 41
        assert digest(arm / "tested-source.json") == digest(ROOT / "results/iteration16/tested-source.json")
        assert digest(arm / "full-tests.txt") == tested["log_sha256"]
        frozen = post["arms"][arm_id]
        assert frozen["run_frozen_expected_sha256"] == suite["frozen_sha256"]
        for phase in ("before_models", "after_models"):
            verify_recorded_inventory(frozen[f"frozen_files_{phase}"], suite["frozen_sha256"])
        references = {name: r["reference"] for name, r in preflight["model_files"].items()}
        assert len(references) == 10 and all(r["reference"] == r["observed"] for r in preflight["model_files"].values())
        model_refs.append(references)
        audit, semantic, supplement = [read(arm / name) for name in ("audit.json", "semantic-repair-audit.json", "supplementary-log-review.json")]
        for receipt in (audit, semantic, supplement):
            verify(receipt["evidence_sha256"])
        verify(semantic["implementation_source_sha256"])
        assert audit["failed_checks"] == supplement["failed_checks"] == EXPECTED_FAILURES[arm_id]
        recomputed = [{"name": r["name"], "checks": sorted(k for k, v in r["checks"].items() if v is not True)}
                      for r in audit["trials"] if any(v is not True for v in r["checks"].values())]
        assert recomputed == audit["failed_checks"]
        assert all(audit["suite_checks"].values()) and not audit["current_frozen_input_mismatches"]
        assert audit["summary"]["observed_effects"] == audit["summary"]["expected_effects"] == 10
        assert semantic["summary"]["declared_intents"] == 10 and semantic["failed_checks"] == []
        stage, analysis = [read(arm / name) for name in ("stt-stage-independent-audit.json", "stt-stage-analysis.json")]
        assert stage["artifact_consistency_checks_pass"] and stage["consistency_issues"] == []
        for receipt in (stage, analysis):
            verify(receipt["source_sha256"], arm); verify(receipt["verified_pcm_sha256"], arm)
        speech, asr, supervision = [read(arm / name) for name in ("received-audio-review/qualitative-review.json", "received-audio-review/report.json", "received-audio-supervision.json")]
        verify(speech["input_sha256"], arm); verify(speech["raw_and_resampled_sha256"], arm)
        verify(asr["source_sha256"])
        assert asr["status"] == "completed" and asr["inputs_unchanged"] and asr["source_unchanged"] and asr["suite_unchanged"]
        assert digest(arm / "received-audio-review/protocol.json") == asr["protocol_sha256"]
        assert [r["id"] for r in asr["trials"]] == TRIALS
        assert all(r["attempted"] and r["status"] == "completed" for r in asr["trials"])
        assert supervision["status"] == "exited" and supervision["exit_code"] == 0 and supervision["finished_at_utc"]
        speech_failures = [r for r in speech["trials"] if not r["manual_review"]["complete_declared_final_speech"]]
        latency, completion = [read(arm / name) for name in ("latency-summary.json", "completion-latency.json")]
        verify(latency["source_sha256"], arm); verify(completion["source_sha256"], arm)
        verify_latency(audit, latency)
        verify_completion(protocol, audit, speech, latency, completion)
        compared = next(r for r in comparison["arms"] if r["arm_id"] == arm_id)
        assert compared["audit_summary"] == audit["summary"] and compared["failed_checks"] == audit["failed_checks"]
        assert len(compared["cases"]) == 4 and len(compared["turns"]) == 10
        for c, observed in zip(compared["cases"], completion["per_case"]):
            assert c["trial"] == observed["trial"] and c["fulfilled_reply_ms"] == observed["fulfilled_case_reply_finish_ms"]
        for role in ("livekit", "llm", "worker"):
            launch = read(arm / "local-stack-run1" / f"{role}-launch.json")
            cleanup = next(r for r in suite["cleanup"] if r["role"] == role)
            assert launch["pid"] == cleanup["pid"]
            owners.append({"arm_id": arm_id, "role": role, "pid": launch["pid"]})
        assert [r["name"] for r in suite["runs"]] == TRIALS
        owners.extend({"arm_id": arm_id, "role": r["name"], "pid": r["pid"]} for r in suite["runs"])
        owners.append({"arm_id": arm_id, "role": "received_asr", "pid": supervision["pid"]})
        assert frozen["asr_pid"] == supervision["pid"]
        assert set(frozen["suite_pids"]) == {r["pid"] for r in owners if r["arm_id"] == arm_id and r["role"] != "received_asr"}
        arm_rows.append({"arm_id": arm_id, "frozen_files_unchanged": 41, "core_summary": audit["summary"],
                         "failed_core_checks": audit["failed_checks"], "semantic_summary": semantic["summary"],
                         "semantic_failed_checks": semantic["failed_checks"], "stt_counts": stage["counts"],
                         "speech_counts": speech["counts"], "failed_final_speech_cases": speech_failures,
                         "completion_groups": completion["groups"], "all_interruption_rows": latency["interruptions"],
                         "interruption_metrics": latency["interruption_metrics"], "runtime_summary": supplement["summary"]})
    assert model_refs[0] == model_refs[1] == model_refs[2]
    verify_recorded_inventory(post["model_files"], model_refs[0])
    owner_set = {(r["arm_id"], r["role"], r["pid"]) for r in owners}
    assert len(owners) == len(owner_set) == 24
    for phase in ("before_models", "after_models"):
        rows = post[f"process_observations_{phase}"]
        assert len(rows) == 24 and {(r["arm_id"], r["role"], r["pid"]) for r in rows} == owner_set
        assert all(r["absent"] and r["returncode"] == 1 and not r["stdout"].strip() and not r["stderr"].strip() for r in rows)
    absent = []
    for owner in owners:
        result = subprocess.run(["ps", "-p", str(owner["pid"]), "-o", "pid=,ppid=,stat=,command="], capture_output=True, text=True, timeout=5)
        assert result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip(), owner
        absent.append({**owner, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "absent": True})
    directories = [BASE, *(ROOT / "results" / name for _, name, _ in ARMS)]
    scripts = [p for d in directories for p in d.rglob("*.py") if not {"source-snapshot", "baseline-source", "__pycache__"}.intersection(p.parts)]
    for path in scripts:
        ast.parse(path.read_text(), filename=str(path))
    subprocess.run(["git", "diff", "--check"], cwd=ROOT, check=True)
    docs = [ROOT / "README.md", ROOT / "docs/iteration17.md", ROOT / "docs/completion-audit.md", ROOT.parent / "prism-pr-description.md"]
    excluded = {target, BASE / "final-validation-output.txt"}
    files = [p for d in directories for p in d.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p not in excluded] + docs
    hashes = {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p): digest(p) for p in sorted(set(files))}
    # Recheck every listed artifact before publishing the receipt; none is an open stdout sink.
    verify(hashes)
    receipt = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "checkpoint_verified_with_known_failures", "task_complete": False,
        "scope": "Frozen source and evidence checkpoint, not quality success or completion of the broader objective. All known interruption/trace/runtime failures remain recorded; no cache change adopted.",
        "tests": tested["tests"], "tests_inherited_from_iteration16": True, "tests_rerun": False,
        "tested_source_files_unchanged": 76, "source_files_snapshotted": 150,
        "post_inference_all_phases_explicitly_verified": True, "post_model_files_matching_stable": 10,
        "post_pid_observations_verified_per_phase": 24, "fresh_owned_pid_observations": absent,
        "model_boundary": "Recorded full-byte/stable model reads are checked against every preflight reference. This finalizer does not reload or rehash model files.",
        "declared_denominators": declared, "screen_decision": comparison["screen_decision"], "arms": arm_rows,
        "failed_core_check_count": sum(len(r["checks"]) for arm in arm_rows for r in arm["failed_core_checks"]),
        "completion_and_interruption_vectors_verified": True,
        "latency_boundary": "Interruption requested is a handle state, not timely or physical audible cutoff. Every PCM tail is retained, including A2_return cancel1 4917.446ms and B1 repeat2 second interruption 7874.679ms.",
        "runtime_summary": rtc_review["summary"], "experiment_python_files_parsed": len(scripts), "git_diff_check": "passed",
        "documentation_sha256": {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p): digest(p) for p in docs},
        "output_capture_boundary": "Stdout must be captured in memory; regular-file stdout is rejected. final-validation-output.txt is created only after exit, excluded together with this self-referential receipt. All other existing artifacts, including historical failed receipts, are hashed.",
        "sha256": hashes,
        "delivery": {"branch": git("branch", "--show-current"), "head": git("rev-parse", "HEAD"),
                     "commits_ahead": int(git("rev-list", "--count", "origin/main..HEAD")),
                     "staged_files": git("diff", "--cached", "--name-only").splitlines(),
                     "status": "No commit, push or PR created by this verifier; publication still requires user-selected staging."},
        "remaining": ["Candidate one-entry quality failure; two-entry setting retained without causal allocation claim",
                      "Anonymous packet attribution and upstream job-status boundaries",
                      "Independent acoustic, real microphone/echo, concurrent-room and adapted benchmark validation",
                      "User-selected staging and PR publication; organizer Theme05 brief access"]}
    with target.open("x") as stream:
        json.dump(receipt, stream, indent=2); stream.write("\n")
    print(json.dumps({k: receipt[k] for k in ("status", "task_complete", "tested_source_files_unchanged", "source_files_snapshotted", "failed_core_check_count", "screen_decision", "delivery")}, indent=2))


if __name__ == "__main__":
    main()
