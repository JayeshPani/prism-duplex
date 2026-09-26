"""Verify the shared cache experiment only after every timed arm and ASR exits."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
ARMS = (
    ("A2_first", "iteration17-cache2-before", 2),
    ("B1", "iteration17-cache1", 1),
    ("A2_return", "iteration17-cache2-after", 2),
)
TRIALS = ("rtc-cancel-1", "rtc-repeat-1", "rtc-repeat-2", "rtc-cancel-2")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def stable_stat(value):
    return {name: getattr(value, name) for name in
            ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}


def read_digest(path, expected=None):
    """One streaming read; detect changes to the open file and its path."""
    row = {"expected": expected, "observed": None, "matches": False, "bytes_read": 0,
           "stable_stat_during_read": False}
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            before = stable_stat(os.fstat(stream.fileno()))
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
                row["bytes_read"] += len(block)
            after = stable_stat(os.fstat(stream.fileno()))
        path_after = stable_stat(Path(path).stat())
        row.update(observed=digest.hexdigest(), stat_before=before, stat_after=after,
                   path_stat_after=path_after,
                   stable_stat_during_read=before == after == path_after
                   and row["bytes_read"] == before["st_size"])
        row["matches"] = row["stable_stat_during_read"] and (
            expected is None or row["observed"] == expected)
    except Exception as error:
        row["error"] = repr(error)
    return row


def check_pids(owners):
    observations = []
    for owner in owners:
        row = {**owner, "checked_at_utc": stamp(), "absent": False}
        command = ["ps", "-p", str(owner["pid"]), "-o", "pid=,ppid=,stat=,command="]
        row["command"] = command
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            row.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
            row["absent"] = result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()
        except Exception as error:
            row["error"] = repr(error)
        observations.append(row)
    return observations


def main():
    output = BASE / "post-inference-validation.json"
    started = time.monotonic()
    report = {"started_at_utc": stamp(), "status": "running", "issues": [], "arms": {},
              "model_files": {}, "evidence_sha256": {},
              "scope": "One fresh full-byte read of each of ten shared model references after all three timed arms and all twelve whole-stream ASR attempts; the same post-read is shared by all arms.",
              "limits": "Reference-byte agreement is not publisher provenance or proof that an earlier process loaded these bytes. PID absence is a point-in-time check and fails closed for a reused PID. Ordering uses retained UTC receipt times, not an independent clock. This verifies evidence consistency and cleanup, not experiment quality or task completion."}
    # Refuse to replace a prior success, failure, or interrupted verification receipt.
    with output.open("x") as stream:
        json.dump(report, stream, indent=2)

    def require(condition, message):
        if not condition:
            report["issues"].append(message)
        return condition

    def read_json(path):
        path = Path(path)
        raw = path.read_bytes()
        report["evidence_sha256"][str(path.relative_to(ROOT))] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    def clock(value):
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("Receipt timestamp must include its UTC offset")
        return result

    def inventory_check(mapping):
        rows = {name: read_digest(name, expected) for name, expected in mapping.items()}
        for name, row in rows.items():
            require(row["matches"], f"File mismatch or unstable read: {name}")
        return rows

    try:
        report["method_sha256"] = read_digest(Path(__file__))["observed"]
        snapshot = read_json(BASE / "source-snapshot.json")
        entries = snapshot["files"]
        require(len(entries) == 150, f"Expected 150 global snapshot entries, observed {len(entries)}")
        original_map = {str(ROOT / row["original"]): row["sha256"] for row in entries}
        snapshot_map = {str(ROOT / row["snapshot"]): row["sha256"] for row in entries}
        require(len(original_map) == len(snapshot_map) == 150, "Duplicate or missing global snapshot paths")
        reference_maps, owners, intervals = [], [], []
        for arm_id, directory, cache_entries in ARMS:
            arm = ROOT / "results" / directory
            preflight = read_json(arm / "preflight.json")
            suite = read_json(arm / "run-report.json")
            supervision = read_json(arm / "received-audio-supervision.json")
            review = read_json(arm / "received-audio-review/report.json")
            row = report["arms"][arm_id] = {"directory": str(arm.relative_to(ROOT)),
                    "cache_entries": cache_entries, "suite_status": suite["status"],
                    "review_status": review["status"], "review_summary": review.get("summary"),
                    "asr_exit_code": supervision.get("exit_code"),
                    "run_frozen_expected_sha256": suite["frozen_sha256"], "suite_pids": []}
            require(preflight["status"] == "passed", f"{arm_id}: preflight did not pass")
            require(preflight["arm_id"] == suite["arm_id"] == arm_id, f"{arm_id}: arm identity mismatch")
            require(preflight["cache_entries"] == suite["cache_entries"] == cache_entries,
                    f"{arm_id}: cache-entry declaration mismatch")
            references = {name: item["reference"] for name, item in preflight["model_files"].items()}
            reference_maps.append(references)
            require(len(references) == 10, f"{arm_id}: expected ten model references")
            require(all(item["reference"] == item["observed"] for item in preflight["model_files"].values()),
                    f"{arm_id}: preflight model mismatch")
            require(len(suite["frozen_sha256"]) == 41, f"{arm_id}: expected 41 actual run-frozen entries")
            require(suite["status"] not in {"starting", "running"} and bool(suite.get("finished_at")),
                    f"{arm_id}: timed suite not terminal")
            require(supervision["status"] == "exited" and bool(supervision.get("finished_at_utc")),
                    f"{arm_id}: received ASR supervisor not terminal")
            require(review["status"] not in {"starting", "running"} and bool(review.get("finished_at_utc")),
                    f"{arm_id}: received ASR report not terminal")
            require(tuple(item["name"] for item in suite["runs"]) == TRIALS,
                    f"{arm_id}: four timed trials/order differ")
            require(tuple(item["id"] for item in review["trials"]) == TRIALS,
                    f"{arm_id}: four received ASR trials/order differ")
            require(all(item.get("attempted") is True for item in review["trials"]),
                    f"{arm_id}: not every whole-stream ASR was attempted")
            cleanup = {item["role"]: item for item in suite["cleanup"]}
            require(set(cleanup) == {"livekit", "llm", "worker"}, f"{arm_id}: missing stack cleanup rows")
            for role in ("livekit", "llm", "worker"):
                launch_path = arm / "local-stack-run1" / f"{role}-launch.json"
                launch = read_json(launch_path)
                require(cleanup[role]["pid"] == launch["pid"], f"{arm_id}/{role}: cleanup PID differs from launch")
                require(cleanup[role].get("observed_command") == launch["command"],
                        f"{arm_id}/{role}: cleanup command differs from launch")
                owners.append({"arm_id": arm_id, "role": role, "pid": launch["pid"],
                               "evidence": str(launch_path.relative_to(ROOT))})
                row["suite_pids"].append(launch["pid"])
            for trial in suite["runs"]:
                owners.append({"arm_id": arm_id, "role": trial["name"], "pid": trial["pid"],
                               "evidence": str((arm / "run-report.json").relative_to(ROOT))})
                row["suite_pids"].append(trial["pid"])
            row["asr_pid"] = supervision["pid"]
            owners.append({"arm_id": arm_id, "role": "received_asr", "pid": supervision["pid"],
                           "evidence": str((arm / "received-audio-supervision.json").relative_to(ROOT))})
            interval = {"arm_id": arm_id, "timed_start": suite["started_at"], "timed_end": suite["finished_at"],
                        "asr_start": supervision["started_at_utc"], "asr_end": supervision["finished_at_utc"],
                        "asr_report_start": review["started_at_utc"], "asr_report_end": review["finished_at_utc"]}
            intervals.append(interval)
            require(clock(interval["timed_start"]) <= clock(interval["timed_end"]), f"{arm_id}: inverted timed interval")
            require(clock(interval["asr_start"]) <= clock(interval["asr_report_start"])
                    <= clock(interval["asr_report_end"]) <= clock(interval["asr_end"]),
                    f"{arm_id}: inconsistent ASR interval")
        require(reference_maps[0] == reference_maps[1] == reference_maps[2], "Preflight model reference maps differ across arms")
        report["preflight_model_references_identical"] = reference_maps[0] == reference_maps[1] == reference_maps[2]
        require(len(owners) == 24, f"Expected 21 suite plus three ASR PID observations, got {len(owners)}")
        for item in owners:
            require(type(item["pid"]) is int and item["pid"] > 0, f"Invalid grounded PID: {item}")
        require(all(clock(left["timed_end"]) <= clock(right["timed_start"])
                    for left, right in zip(intervals, intervals[1:])), "Timed arms overlapped or ran out of declared order")
        last_timed_end = max(clock(item["timed_end"]) for item in intervals)
        require(all(clock(item["asr_start"]) >= last_timed_end for item in intervals),
                "Received ASR began before the last timed arm ended")
        require(max(clock(item["asr_end"]) for item in intervals) <= clock(report["started_at_utc"]),
                "Verification began before all received ASR supervisors finished")
        report["ordering"] = {"intervals": intervals, "last_timed_end": last_timed_end.isoformat(),
                              "basis": "Retained UTC receipt timestamps"}
        report["process_observations_before_models"] = check_pids(owners)
        require(all(item["absent"] for item in report["process_observations_before_models"]),
                "A grounded PID is still present or its absence cannot be verified")
        # No large model reads unless every completion, ordering and PID gate passes.
        if report["issues"]:
            raise RuntimeError("Completion/reference/PID gates failed; model reads skipped")
        report["global_originals_before_models"] = inventory_check(original_map)
        report["global_snapshots_before_models"] = inventory_check(snapshot_map)
        for row in report["arms"].values():
            row["frozen_files_before_models"] = inventory_check(row["run_frozen_expected_sha256"])
        if report["issues"]:
            raise RuntimeError("Frozen-source checks failed; model reads skipped")
        report["model_read_started_at_utc"] = stamp()
        report["model_files"] = inventory_check(reference_maps[0])
        report["model_read_finished_at_utc"] = stamp()
        report["global_originals_after_models"] = inventory_check(original_map)
        report["global_snapshots_after_models"] = inventory_check(snapshot_map)
        for row in report["arms"].values():
            row["frozen_files_after_models"] = inventory_check(row["run_frozen_expected_sha256"])
        report["process_observations_after_models"] = check_pids(owners)
        require(all(item["absent"] for item in report["process_observations_after_models"]),
                "A grounded PID is present or absence cannot be verified after model reads")
        # Reports/manifests and the verifier must also remain stable during verification.
        report["evidence_recheck"] = inventory_check({str(ROOT / name): digest
                                                     for name, digest in report["evidence_sha256"].items()})
        require(read_digest(Path(__file__), report["method_sha256"])["matches"], "Verifier changed during execution")
    except Exception as error:
        report["fatal_error"] = repr(error)
        report["issues"].append(f"Verification could not complete: {error}")
    finally:
        report["checked_at_utc"] = stamp()
        report["elapsed_seconds"] = time.monotonic() - started
        report["status"] = "passed" if not report["issues"] else "failed"
        report["summary"] = {"model_files_read_once": len(report["model_files"]),
                             "model_files_matching": sum(row["matches"] for row in report["model_files"].values()),
                             "global_snapshot_entries": len(report.get("global_originals_before_models", {})),
                             "run_frozen_entries_by_arm": {arm: len(row["run_frozen_expected_sha256"])
                                                           for arm, row in report["arms"].items()},
                             "pid_observations_before": len(report.get("process_observations_before_models", [])),
                             "pid_observations_after": len(report.get("process_observations_after_models", [])),
                             "issue_count": len(report["issues"])}
        output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "summary": report["summary"], "issues": report["issues"]}))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
