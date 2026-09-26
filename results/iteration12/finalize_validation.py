"""Validate this diagnostic checkpoint while retaining all failed scenarios."""
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
    mismatches = [name for name, expected in mapping.items() if digest(root / name) != expected]
    assert not mismatches, mismatches
    return len(mapping)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def main():
    target = BASE / "final-validation.json"
    assert not target.exists(), "Preserve the existing checkpoint."
    tested = json.loads((ROOT / "results/iteration11/tested-source.json").read_text())
    tested_count = verify(tested["paths_sha256"])
    test_log = (BASE / "stt-diagnostics-tests-1.txt").read_text()
    assert "6 passed in 1.10s" in test_log
    before, after = [json.loads(next(line[len(prefix):] for line in test_log.splitlines()
                                    if line.startswith(prefix)))
                     for prefix in ("SOURCE_HASHES_BEFORE ", "SOURCE_HASHES_AFTER ")]
    assert before == after
    verify(after)
    preflight = read("preflight.json")
    verify(preflight["source_input_method_sha256"])
    suite = read("run-report.json")
    assert suite["status"] == "completed_with_failed_captures"
    assert suite["frozen_inputs_unchanged"]
    frozen_count = verify(suite["frozen_sha256"])
    post = read("post-inference-validation.json")
    assert len(post["model_files"]) == 10 and all(row["matches"] for row in post["model_files"].values())
    audit, original = read("audit-v2.json"), read("audit.json")
    verify(audit["evidence_sha256"])
    for earlier in original["trials"]:
        if "turns" in earlier:
            current = next(row for row in audit["trials"] if row["name"] == earlier["name"])
            assert current["turns"] == earlier["turns"]
            assert current["interruption"] == earlier["interruption"]
    supplement = read("supplementary-log-review.json")
    verify(supplement["evidence_sha256"])
    verify(read("readiness-boundary-audit.json")["evidence_sha256"])
    stage = read("stt-stage-independent-audit.json")
    assert stage["all_checks_pass"]
    verify(stage["source_sha256"], BASE)
    speech = read("received-audio-review/qualitative-review.json")
    verify(speech["input_sha256"], BASE)
    snapshots = read("source-snapshot.json")
    for row in snapshots["files"]:
        assert digest(ROOT / row["snapshot"]) == row["sha256"] == digest(ROOT / row["original"])
    pids = {row["pid"] for row in read("pre-asr-cleanup.json")["observations"]}
    pids.add(read("received-audio-supervision.json")["pid"])
    absent = {str(pid): not psutil.pid_exists(pid) for pid in sorted(pids)}
    assert all(absent.values()), absent
    scripts = [path for path in BASE.rglob("*.py") if "source-snapshot" not in path.parts]
    for path in scripts:
        ast.parse(path.read_text(), filename=str(path))
    subprocess.run(["git", "diff", "--check"], cwd=ROOT, check=True)
    files = [path for path in BASE.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    files += [ROOT / "README.md", ROOT / "docs/iteration12.md", ROOT.parent / "prism-pr-description.md"]
    receipt = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Integrity checkpoint; known capture and server-status failures are retained.",
        "production_test_receipt": "iteration11: " + tested["tests"],
        "tested_source_files_unchanged": tested_count,
        "diagnostic_tests": "6 passed in 1.10s; helper/test/production-STT hashes unchanged",
        "rtc_frozen_files_unchanged": frozen_count,
        "source_files_snapshotted": len(snapshots["files"]),
        "experiment_python_files_parsed": len(scripts),
        "git_diff_check": "passed",
        "rtc_summary": audit["summary"],
        "failed_checks": audit["failed_checks"],
        "original_audit_error_preserved": [row for row in original["trials"] if "audit_error" in row],
        "completed_trial_metrics_unchanged_by_corrected_audit": True,
        "stt_stage_audit_counts": stage["counts"],
        "whole_stream_review_counts": speech["counts"],
        "log_census": supplement["summary"],
        "pre_and_post_model_references_match": 10,
        "owned_pids_absent": absent,
        "delivery": {"branch": git("branch", "--show-current"), "head": git("rev-parse", "HEAD"),
                     "commits_ahead": int(git("rev-list", "--count", "origin/main..HEAD")),
                     "staged_files": git("diff", "--cached", "--name-only").splitlines(),
                     "status": "No commit, push or PR created; user-selected staging required."},
        "sha256": {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): digest(path)
                   for path in sorted(set(files))},
        "remaining": ["Trusted recorder readiness under delayed participant lookup",
                      "Generation variance and recognition/interruptibility reliability",
                      "Upstream acknowledged graceful-job completion",
                      "User-selected staging and PR publication", "Organizer Theme 05 brief access",
                      "Human microphone/echo and physical playback acceptance",
                      "Live cancellation/repeat visits, concurrent rooms and fresh adapted FDB evaluation"],
    }
    target.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({key: receipt[key] for key in (
        "production_test_receipt", "diagnostic_tests", "rtc_frozen_files_unchanged",
        "source_files_snapshotted", "experiment_python_files_parsed", "delivery")}, indent=2))


if __name__ == "__main__":
    main()
