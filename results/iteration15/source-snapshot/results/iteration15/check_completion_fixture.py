"""Check failed-case nulls using frozen iteration14 artifacts in a temporary copy."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BASE = Path(__file__).resolve().parent
PREVIOUS = BASE.parent / "iteration14"


def main():
    with tempfile.TemporaryDirectory(prefix="prism-completion-metric-") as directory:
        root = Path(directory)
        (root / "received-audio-review").mkdir()
        data = ["protocol.json", "audit.json", "latency-summary.json",
                "received-audio-review/qualitative-review.json"]
        methods = ["summarize_completion.py", "summarize_latency.py"]
        for name in data:
            shutil.copyfile(PREVIOUS / name, root / name)
        for name in methods:
            shutil.copyfile(BASE / name, root / name)
        process = subprocess.run([sys.executable, str(root / methods[0])], capture_output=True, text=True)
        assert process.returncode == 0, process.stderr
        report = json.loads((root / "completion-latency.json").read_text())
        assert report["groups"]["all"]["declared"] == 4 and report["groups"]["all"]["n"] == 2
        assert report["groups"]["repeat"]["declared"] == 2 and report["groups"]["repeat"]["n"] == 0
        assert report["groups"]["all"]["failed_or_unmeasured"] == 2
        assert all(row["observed_finished_reply_ms"] is not None for row in report["per_case"])
        assert [row["fulfilled_case_reply_finish_ms"] is None for row in report["per_case"]] == [False, True, True, False]
        print(json.dumps({"status": "passed", "groups": report["groups"],
                          "scope": "Temporary copies only; no previous artifact changes or inference.",
                          "source_sha256": {name: sha256((BASE / name).read_bytes()).hexdigest() for name in methods}}, indent=2))


if __name__ == "__main__":
    main()
