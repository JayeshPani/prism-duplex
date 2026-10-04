"""Verify a terminal arm's owned PIDs before starting the next arm or speech review."""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm")
    args = parser.parse_args()
    plan = json.loads((BASE / "plan.json").read_text())
    candidates = [row for row in plan["arms"] if row["name"] == args.arm]
    assert len(candidates) == 1
    directory = ROOT / candidates[0]["directory"]
    report = json.loads((directory / "run-report.json").read_text())
    assert report.get("finished_at") and report["status"] not in {"starting", "running"}
    pids = {row["pid"] for row in report["runs"] if "pid" in row}
    pids.update(row["pid"] for row in report["cleanup"])
    observations = []
    for pid in sorted(pids):
        check = subprocess.run(["ps", "-p", str(pid), "-o", "pid=,command="], capture_output=True, text=True)
        observations.append({"pid": pid, "returncode": check.returncode,
                             "stdout": check.stdout, "stderr": check.stderr,
                             "absent": check.returncode == 1 and not check.stdout.strip()})
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "suite_status": report["status"], "observations": observations,
              "all_owned_pids_absent": bool(pids) and all(row["absent"] for row in observations),
              "scope": "Terminal timed arm's owned stack and trial PIDs checked before the next timed arm or any received-speech ASR. Other declared arms may run afterward."}
    with (directory / "pre-asr-cleanup.json").open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({key: result[key] for key in ("suite_status", "all_owned_pids_absent")}))
    return 0 if result["all_owned_pids_absent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
