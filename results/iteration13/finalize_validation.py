"""Freeze the recorder checkpoint without replacing historical evidence."""
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
    preflight, suite = read("preflight.json"), read("run-report.json")
    verify(preflight["source_input_method_sha256"])
    assert suite["status"] == "completed" and suite["frozen_inputs_unchanged"]
    frozen_count = verify(suite["frozen_sha256"])
    post = read("post-inference-validation.json")
    assert len(post["model_files"]) == 10 and all(r["matches"] for r in post["model_files"].values())
    assert all(r["matches"] for r in post["frozen_files"].values())
    audit, supplement = read("audit.json"), read("supplementary-log-review.json")
    verify(audit["evidence_sha256"])
    verify(supplement["evidence_sha256"])
    stage = read("stt-stage-independent-audit.json")
    assert stage["artifact_consistency_checks_pass"]
    verify(stage["source_sha256"], BASE)
    speech = read("received-audio-review/qualitative-review.json")
    verify(speech["input_sha256"], BASE)
    verify(speech["raw_and_resampled_sha256"], BASE)
    snapshots = read("source-snapshot.json")
    for row in snapshots["files"]:
        assert digest(ROOT / row["snapshot"]) == row["sha256"] == digest(ROOT / row["original"])
    pids = {r["pid"] for r in read("pre-asr-cleanup.json")["observations"]}
    pids.add(read("received-audio-supervision.json")["pid"])
    absent = {str(pid): not psutil.pid_exists(pid) for pid in sorted(pids)}
    assert all(absent.values()), absent
    scripts = [p for p in BASE.rglob("*.py") if not {"source-snapshot", "baseline-source"}.intersection(p.parts)]
    for path in scripts:
        ast.parse(path.read_text(), filename=str(path))
    subprocess.run(["git", "diff", "--check"], cwd=ROOT, check=True)
    files = [p for p in BASE.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    files += [ROOT / "README.md", ROOT / "docs/iteration13.md", ROOT.parent / "prism-pr-description.md"]
    receipt = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Source, tests and retained evidence verified; component delayed-attribution proof remains distinct from live compatibility.",
        "tests": tested["tests"], "tested_source_files_unchanged": test_count,
        "rtc_frozen_files_unchanged": frozen_count, "source_files_snapshotted": len(snapshots["files"]),
        "experiment_python_files_parsed": len(scripts), "git_diff_check": "passed",
        "rtc_summary": audit["summary"], "failed_checks": audit["failed_checks"],
        "stt_stage_counts": stage["counts"], "whole_stream_review_counts": speech["counts"],
        "supplementary_summary": supplement["summary"], "pre_and_post_model_references_match": 10,
        "owned_pids_absent": absent,
        "delivery": {"branch": git("branch", "--show-current"), "head": git("rev-parse", "HEAD"),
                     "commits_ahead": int(git("rev-list", "--count", "origin/main..HEAD")),
                     "staged_files": git("diff", "--cached", "--name-only").splitlines(),
                     "status": "No commit, push or PR created by this iteration; user-selected staging required."},
        "sha256": {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p): digest(p)
                   for p in sorted(set(files))},
        "remaining": ["Live reproduction of delayed participant lookup; this suite did not exercise it",
                      "Recognition variance and human microphone/echo/physical playback acceptance",
                      "Live cancellation/repeated visits and concurrent rooms",
                      "Upstream acknowledged graceful completion and fresh adapted FDB evaluation",
                      "User-selected staging for PR publication and organizer Theme 05 brief access"],
    }
    with target.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(json.dumps({k: receipt[k] for k in ("tests", "rtc_frozen_files_unchanged", "source_files_snapshotted", "delivery")}, indent=2))


if __name__ == "__main__":
    main()
