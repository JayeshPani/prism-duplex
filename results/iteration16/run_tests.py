"""Run the complete test suite once and retain source/exit receipts."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def hashes():
    paths = [p for folder in ("agent", "tests", "scripts") for p in (ROOT / folder).rglob("*.py")]
    paths += [ROOT / "agent/config.yaml", ROOT / "pytest.ini"]
    return {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))}


def main():
    target = BASE / "tested-source.json"
    assert not target.exists(), "Preserve the existing tested-source receipt."
    log = BASE / "full-tests.txt"
    before = hashes()
    command = [sys.executable, "-m", "pytest", "-q"]
    with log.open("x") as stream:
        process = subprocess.run(command, cwd=ROOT, text=True, stdout=stream, stderr=subprocess.STDOUT)
    after = hashes()
    output = log.read_text()
    summaries = [line for line in output.splitlines() if re.search(r"\d+ passed|\d+ failed", line)]
    receipt = {"at_utc": datetime.now(timezone.utc).isoformat(), "command": command,
               "exit_code": process.returncode, "tests": summaries[-1] if summaries else "No summary found",
               "paths_sha256": before, "after_sha256": after, "unchanged_during_test": before == after,
               "log_sha256": sha256(log.read_bytes()).hexdigest()}
    with target.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(json.dumps({k: receipt[k] for k in ("tests", "exit_code", "unchanged_during_test")}, indent=2))
    return 0 if process.returncode == 0 and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
