"""Freeze the cancellation/return checkpoint with known failures retained."""
import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
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


def main():
    target = BASE / "final-validation.json"
    assert not target.exists(), "Preserve the existing checkpoint."
    tested = read("tested-source.json")
    assert tested["exit_code"] == 0 and tested["unchanged_during_test"]
    test_count = verify(tested["paths_sha256"])
    assert test_count == 59 and tested["tests"].startswith("575 passed"), tested
    preflight, suite = read("preflight.json"), read("run-report.json")
    verify(preflight["source_input_method_sha256"])
    assert suite["status"] == "completed" and suite["frozen_inputs_unchanged"]
    frozen_count = verify(suite["frozen_sha256"])
    assert frozen_count == 39, frozen_count
    post = read("post-inference-validation.json")
    assert len(post["model_files"]) == 10 and all(r["matches"] for r in post["model_files"].values())
    assert all(r["matches"] for r in post["frozen_files"].values())
    assert set(post["model_files"]) == set(preflight["model_files"])
    assert all(row["expected"] == row["observed"] == preflight["model_files"][name]["reference"]
               for name, row in post["model_files"].items())
    assert set(post["frozen_files"]) == set(suite["frozen_sha256"])
    assert all(row["matches"] for row in post["frozen_files_rechecked_after_models"].values())
    audit, supplement = read("audit.json"), read("supplementary-log-review.json")
    verify(audit["evidence_sha256"])
    verify(supplement["evidence_sha256"])
    known_failed_checks = {"result_after_effect_3", "allowed_plan_3", "effect_count",
                           "no_noop_or_extra_writes", "effect_state_3", "fresh_computed_route_3"}
    assert len(audit["failed_checks"]) == 1
    assert audit["failed_checks"][0]["name"] == "rtc-repeat-1"
    assert set(audit["failed_checks"][0]["checks"]) == known_failed_checks
    assert supplement["failed_checks"] == audit["failed_checks"]
    stage = read("stt-stage-independent-audit.json")
    assert stage["artifact_consistency_checks_pass"]
    verify(stage["source_sha256"], BASE)
    verify(stage["verified_pcm_sha256"], BASE)
    speech = read("received-audio-review/qualitative-review.json")
    verify(speech["input_sha256"], BASE)
    verify(speech["raw_and_resampled_sha256"], BASE)
    speech_failures = [{"trial": row["id"], "facts": row["manual_review"]["facts"],
                       "note": row["manual_review"]["note"],
                       "semantic_effect_case_passed": row["semantic_effect_case_passed"]}
                      for row in speech["trials"] if not row["manual_review"]["complete_declared_final_speech"]]
    assert [row["trial"] for row in speech_failures] == ["rtc-repeat-1", "rtc-repeat-2"]
    assert speech["counts"]["complete_declared_final_speech_facts"] == 2
    snapshots = read("source-snapshot.json")
    assert len(snapshots["files"]) == 75
    for row in snapshots["files"]:
        assert digest(ROOT / row["snapshot"]) == row["sha256"] == digest(ROOT / row["original"])
    pids = {r["pid"] for r in read("pre-asr-cleanup.json")["observations"]}
    pids.add(read("received-audio-supervision.json")["pid"])
    assert len(pids) == 8
    absent = {str(pid): not psutil.pid_exists(pid) for pid in sorted(pids)}
    assert all(absent.values()), absent
    scripts = [p for p in BASE.rglob("*.py") if not {"source-snapshot", "baseline-source"}.intersection(p.parts)]
    for path in scripts:
        ast.parse(path.read_text(), filename=str(path))
    subprocess.run(["git", "diff", "--check"], cwd=ROOT, check=True)
    files = [p for p in BASE.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    documentation = [ROOT / "README.md", ROOT / "docs/iteration14.md", ROOT.parent / "prism-pr-description.md", ROOT / "docs/completion-audit.md"]
    assert all(path.is_file() for path in documentation)
    files += documentation
    preparation_failures = sorted(path for path in (BASE / "preparation-failures").rglob("*") if path.is_file())
    assert preparation_failures
    receipt = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "checkpoint_verified_with_known_failures", "task_complete": False,
        "scope": "Source, tests and retained evidence verified at a checkpoint. Six core checks and two final spoken-content cases remain failed; capture completion is not fulfilled user intent or task completion.",
        "tests": tested["tests"], "tested_source_files_unchanged": test_count,
        "rtc_frozen_files_unchanged": frozen_count, "source_files_snapshotted": len(snapshots["files"]),
        "experiment_python_files_parsed": len(scripts), "git_diff_check": "passed",
        "rtc_summary": audit["summary"], "failed_checks": audit["failed_checks"],
        "failed_core_check_count": sum(len(row["checks"]) for row in audit["failed_checks"]),
        "failed_final_speech_cases": speech_failures,
        "stt_stage_counts": stage["counts"], "whole_stream_review_counts": speech["counts"],
        "supplementary_summary": supplement["summary"], "pre_and_post_model_references_match": 10,
        "model_verification_boundary": "The fresh full-byte model hashes are in post-inference-validation.json; this finalizer verifies that receipt against preflight references without loading models or rehashing weights again.",
        "owned_pids_absent": absent,
        "documentation_sha256": {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): digest(path) for path in documentation},
        "preparation_failure_artifacts": {str(path.relative_to(ROOT)): digest(path) for path in preparation_failures},
        "syntax_check_boundary": "Only current experiment *.py files are parsed, excluding source-snapshot and baseline-source. The preserved deliberately broken preparation source is *.py.txt, retained and hashed without AST parsing.",
        "delivery": {"branch": git("branch", "--show-current"), "head": git("rev-parse", "HEAD"),
                     "commits_ahead": int(git("rev-list", "--count", "origin/main..HEAD")),
                     "staged_files": git("diff", "--cached", "--name-only").splitlines(),
                     "status": "No commit, push or PR created by this iteration; user-selected staging required."},
        "sha256": {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p): digest(p)
                   for p in sorted(set(files))},
        "remaining": ["First return trajectory omitted start_navigation after computing a route; only nine of ten declared effects occurred",
                      "Both return trajectories failed declared final speech facts; the otherwise successful return omitted the replaced-office fact",
                      "Live reproduction of delayed participant lookup; this suite did not exercise it",
                      "Recognition variance and human microphone/echo/physical playback acceptance",
                      "Reliable repeated visits beyond these small development cases and concurrent rooms",
                      "Upstream acknowledged graceful completion and fresh adapted FDB evaluation",
                      "User-selected staging for PR publication and organizer Theme 05 brief access"],
    }
    with target.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(json.dumps({k: receipt[k] for k in ("tests", "rtc_frozen_files_unchanged", "source_files_snapshotted", "delivery")}, indent=2))


if __name__ == "__main__":
    main()
