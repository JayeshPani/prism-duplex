"""Snapshot all evaluated Python source, config, tests and experiment methods."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def main():
    target = BASE / "source-snapshot.json"
    assert not target.exists()
    tested = json.loads((BASE / "tested-source.json").read_text())
    paths = {ROOT / name for name in tested["paths_sha256"]}
    paths.update(BASE.glob("*.py"))
    paths.update([ROOT / "agent/config.yaml", ROOT / "pytest.ini", ROOT / "results/iteration11/instrumented_worker.py"])
    rows = []
    for path in sorted(paths):
        name = path.relative_to(ROOT)
        destination = BASE / "source-snapshot" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        assert not destination.exists()
        shutil.copyfile(path, destination)
        expected = sha256(path.read_bytes()).hexdigest()
        assert sha256(destination.read_bytes()).hexdigest() == expected
        if str(name) in tested["paths_sha256"]:
            assert tested["paths_sha256"][str(name)] == expected
        rows.append({"original": str(name), "snapshot": str(destination.relative_to(ROOT)), "sha256": expected})
    receipt = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
               "scope": "Exact evaluated agent, test, scripts and experiment Python files plus configs. Restore each snapshot to original path; model/runtime receipts are separate.",
               "baseline_checkpoint": "results/iteration14/final-validation.json", "files": rows}
    with target.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(f"Preserved {len(rows)} exact source files.")


if __name__ == "__main__":
    main()
