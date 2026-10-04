"""Freeze all three cache arms before the first preflight or service launch."""
from datetime import datetime, timezone
from hashlib import sha256
import ast
import json
from pathlib import Path
import shutil

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def main():
    target = BASE / "source-snapshot.json"
    assert not target.exists(), "Preserve the existing freeze."
    plan = json.loads((BASE / "plan.json").read_text())
    assert digest(ROOT / plan["baseline_checkpoint"]["path"]) == plan["baseline_checkpoint"]["sha256"]
    tested_path = ROOT / "results/iteration16/tested-source.json"
    tested = json.loads(tested_path.read_text())
    assert tested["exit_code"] == 0 and tested["unchanged_during_test"]
    assert tested["paths_sha256"] == tested["after_sha256"]
    assert tested["tests"].startswith("655 passed")
    assert all(digest(ROOT / path) == expected for path, expected in tested["paths_sha256"].items())
    paths = {ROOT / path for path in tested["paths_sha256"]}
    paths.update([ROOT / "agent/config.yaml", ROOT / "pytest.ini",
                  ROOT / "results/iteration11/instrumented_worker.py", tested_path,
                  ROOT / "results/iteration16/local-stack-run1/llm-launch.json",
                  ROOT / "results/iteration16/local-stack-run1/livekit.yaml",
                  ROOT / "results/iteration16/post-inference-validation.json"])
    method_hashes = []
    for arm in plan["arms"]:
        directory = ROOT / arm["directory"]
        assert not (directory / "preflight.json").exists()
        assert not (directory / "local-stack-run1").exists()
        assert digest(directory / "tested-source.json") == digest(tested_path)
        assert digest(directory / "full-tests.txt") == tested["log_sha256"]
        methods = sorted(directory.glob("*.py"))
        assert methods
        method_hashes.append({path.name: digest(path) for path in methods})
        paths.update(methods)
        paths.update(directory.glob("*.json"))
        paths.update(directory.glob("*.txt"))
        paths.update((directory / "audio-assets").glob("*.wav"))
    assert method_hashes[0] == method_hashes[1] == method_hashes[2], "All arm methods must be identical."
    assert (BASE / "comparison-protocol.json").is_file()
    paths.update(BASE.glob("*.py"))
    paths.update(BASE.glob("*.json"))
    rows = []
    for path in sorted(paths):
        if path.suffix == ".py":
            ast.parse(path.read_text(), filename=str(path))
        name = path.relative_to(ROOT)
        destination = BASE / "source-snapshot" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        assert not destination.exists()
        shutil.copyfile(path, destination)
        expected = digest(path)
        assert digest(destination) == expected
        rows.append({"original": str(name), "snapshot": str(destination.relative_to(ROOT)), "sha256": expected})
    receipt = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Exact source, inherited tests, all arm methods/protocols/audio and global predeclared comparison; no model service has started.",
        "inherited_test_boundary": "The 655 tests ran in iteration16, not again here. All 76 tested source files are unchanged before freezing; original receipts and dates are retained.",
        "baseline_checkpoint": plan["baseline_checkpoint"],
        "arm_methods_byte_identical": True,
        "arm_method_sha256": method_hashes[0],
        "files": rows,
    }
    with target.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(f"Preserved {len(rows)} source, protocol, input and method files; inherited tested source unchanged.")


if __name__ == "__main__":
    main()
