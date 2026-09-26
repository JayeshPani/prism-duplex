"""Freeze the delivery checkpoint without rerunning inference or staging files."""
from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess

import psutil


ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def read(name):
    return json.loads((OUT / name).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def verify(mapping):
    mismatches = [name for name, expected in mapping.items()
                  if sha(ROOT / name) != expected]
    assert not mismatches, mismatches
    return len(mapping)


def main():
    destination = OUT / "final-validation.json"
    assert not destination.exists(), "Use a new checkpoint; do not overwrite."
    tested = read("tested-source.json")
    tested_count = verify(tested["paths_sha256"])
    assert "555 passed in 5.39s" in (OUT / "full-suite.txt").read_text()
    run = read("run-report.json")
    frozen_count = verify(run["frozen_sha256"])
    assert run["status"] == "completed" and run["frozen_inputs_unchanged"]
    audit = read("audit.json")
    verify(audit["evidence_sha256"])
    supplement = read("supplementary-log-review.json")
    verify(supplement["evidence_sha256"])
    review = read("received-audio-review/qualitative-review.json")
    verify({str(OUT / name): value for name, value in review["input_sha256"].items()})
    comparison = read("trace-io-analysis.json")
    for row in comparison["all_trials"]:
        assert all(row["checks"].values())
        assert sha(ROOT / row["file"]) == row["file_sha256"]
    verify({name: row["declared"] for name, row in comparison["source_hashes"].items()})
    aec = read("aec-boundary-reproduction.json")
    verify(aec["source_sha256"])
    post = read("post-inference-validation.json")
    assert len(post["model_hashes"]) == 10
    assert all(row["matches"] for row in post["model_hashes"].values())
    pids = {row["pid"] for row in read("pre-asr-cleanup.json")["observations"]}
    pids.update([read("received-audio-supervision.json")["pid"], 36116])
    process_states = {str(pid): not psutil.pid_exists(pid) for pid in sorted(pids)}
    assert all(process_states.values()), process_states
    scripts = list(OUT.rglob("*.py"))
    for path in scripts:
        ast.parse(path.read_text(), filename=str(path))
    whitespace = subprocess.run(["git", "diff", "--check"], cwd=ROOT,
                                text=True, capture_output=True)
    assert whitespace.returncode == 0, whitespace.stdout + whitespace.stderr
    raw_dirs = [ROOT / "results/iteration9/eta-speech/raw",
                ROOT / "results/iteration10/number-speech/raw"]
    raw = sorted(path for directory in raw_dirs for path in directory.iterdir()
                 if path.is_file())
    visible = set(git("ls-files", "--others", "--exclude-standard").splitlines())
    tracked = set(git("ls-files").splitlines())
    assert len(raw) == 228
    assert all(str(path.relative_to(ROOT)) in visible | tracked for path in raw)
    files = [path for path in OUT.rglob("*") if path.is_file()
             and "__pycache__" not in path.parts]
    files += raw + [ROOT / ".gitignore", ROOT / "README.md",
                    ROOT / "docs/iteration11.md", ROOT.parent / "prism-pr-description.md"]
    receipt = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Integrity and delivery checkpoint, not an all-scenarios-pass claim.",
        "tests": tested["tests"],
        "tested_source_files_still_matching": tested_count,
        "rtc_frozen_files_still_matching": frozen_count,
        "experiment_python_files_parsed": len(scripts),
        "git_diff_check": "passed",
        "rtc_summary": audit["summary"],
        "rtc_failed_checks_preserved": audit["failed_checks"],
        "runtime_log_summary": supplement["summary"],
        "whole_stream_review_counts": review["counts"],
        "component_comparison": comparison["denominators"],
        "post_inference_model_hash_matches": len(post["model_hashes"]),
        "missing_preflight": post["pre_rtc_hash_limitation"],
        "review_method_timing": review["review_method_timing"],
        "owned_pids_absent": process_states,
        "delivery": {
            "branch": git("branch", "--show-current"),
            "head": git("rev-parse", "HEAD"),
            "base": git("rev-parse", "origin/main"),
            "commits_ahead": int(git("rev-list", "--count", "origin/main..HEAD")),
            "staged_files": git("diff", "--cached", "--name-only").splitlines(),
            "status": "Awaiting user-selected staging; no commit, push or PR created.",
            "newly_visible_frozen_raw_files": len(raw),
            "newly_visible_frozen_raw_bytes": sum(path.stat().st_size for path in raw),
        },
        "sha256": {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): sha(path)
                   for path in sorted(set(files))},
        "remaining": [
            "User-selected staging for commit and PR",
            "Slow/missing recognition and incomplete interruption reliability",
            "SDK startup-silencing comparison and real microphone/echo acceptance",
            "LiveKit failed-job status investigation",
            "Organizer Theme 05 brief access",
            "Live cancellation/repeat visits, concurrent rooms and fresh adapted FDB evaluation",
        ],
    }
    destination.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({key: receipt[key] for key in (
        "tests", "tested_source_files_still_matching", "rtc_frozen_files_still_matching",
        "experiment_python_files_parsed", "git_diff_check", "delivery")}, indent=2))


if __name__ == "__main__":
    main()
