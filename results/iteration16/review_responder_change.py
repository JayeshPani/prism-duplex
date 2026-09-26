"""Read-only source comparisons accompanying a manual responder review; no models/tests."""
import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def node(source, name):
    return next(item for item in ast.parse(source).body if getattr(item, "name", None) == name)


def main():
    baseline = ROOT / "results/iteration15/source-snapshot/agent/coordinator/responder.py"
    candidate = ROOT / "agent/coordinator/responder.py"
    test = ROOT / "tests/unit/test_redundant_navigation_search.py"
    assert digest(candidate) == "8c28e89d29f897854a9d7c1625e11478e21b4c07a8e751ffb37ff1726f73f508"
    assert digest(test) == "501d53a8c5455c938ee18625c127cefc3ae0e6ad0ea3b36ca0decbea8af73006"
    assert ast.dump(node(baseline.read_text(), "Responder")) == ast.dump(node(candidate.read_text(), "Responder"))
    snapshots = json.loads((ROOT / "results/iteration15/source-snapshot.json").read_text())
    changed = [row["original"] for row in snapshots["files"] if row["original"].startswith("agent/")
               and digest(ROOT / row["original"]) != row["sha256"]]
    assert changed == ["agent/coordinator/responder.py"], changed
    assert "65 passed in 0.46s" in (BASE / "redundant-search-after.txt").read_text()
    assert "6 failed, 32 passed" in (BASE / "redundant-search-before-v2.txt").read_text()
    reviewed = [baseline, candidate, test, ROOT / "tests/unit/test_navigation_summaries.py",
                ROOT / "tests/unit/test_summary_request_fidelity.py", ROOT / "agent/coordinator/refs.py",
                ROOT / "agent/tools/car_tools.py", Path(__file__).resolve(),
                BASE / "redundant-search-before.txt", BASE / "redundant-search-before-v2.txt",
                BASE / "redundant-search-after.txt"]
    receipt = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "reviewer": "coordinator_lifecycle independent agent",
        "verdict": "No concrete blocker found within the narrow redundant-destination-lookup summary change.",
        "baseline": str(baseline.relative_to(ROOT)),
        "changed_production_files": changed,
        "checks": {"expected_candidate_and_test_hashes": True,
                   "responder_summarize_class_ast_unchanged": True,
                   "only_responder_production_changed_from_iteration15": True},
        "manual_findings": [
            "The exception applies only to start_navigation with one successful mutation, complete nonstale outcomes and successful done/reused reads. Every otherwise-unused call must be search_destination; no arbitrary read or second mutation is discarded.",
            "Each exempt search needs a nonempty literal query, exactly one dictionary place with a nonempty name, and a place_id equal to the confirmed nonempty string destination_id. Missing IDs, multiple results, references and differing destinations keep fallback.",
            "The original full command still controls eligibility. Additional questions, list/detail requests and unfamiliar wording go to the unchanged model prompt carrying original request, separately labeled repaired interpretation and all outcomes.",
            "Compute aliases now require their actual result destination_id to match the confirmed destination. An ancestor compute used only as a via-source cannot make its different destination a main-target alias. Search aliases use the same canonical destination test and do not depend on plan list ordering.",
            "Output wording, facts, replacement clause, ETA and 30-word ceiling remain unchanged. Add/cancel do not receive the new unused-search exception; existing unknown/error/blocked/stale/multiple-write handling remains in place.",
            "Read new tests for done/reused and reordered plans, multiple redundant searches, unrelated/unsupported results, original detail requests, via-source alias rejection, snapshot guards, other mutation types and error/unknown/blocked boundaries. Existing original-versus-repaired request tests remain unchanged."
        ],
        "execution_boundary": "This method only hashes files, parses source ASTs, compares the frozen inventory and reads existing test receipts. It does not run tests, tool backends, inference or services. Manual review used the full responder diff and referenced tests/contracts.",
        "test_receipts": {"implementer_baseline": "6 failed, 32 passed against exact iteration15 responder",
                          "implementer_candidate": "65 passed in 0.46s (38 new cases plus 27 existing summary cases)",
                          "initial_collection_failure": "redundant-search-before.txt preserves a reserved pytest parameter-name collection error; it is not the behavioral baseline"},
        "source_sha256": {str(path.relative_to(ROOT)): digest(path) for path in reviewed},
        "limitations": [
            "This is a source review and artifact comparison, not an independently rerun behavioral or live test.",
            "The exception trusts successful canonical IDs reported by the backend; it does not prove arbitrary backend result integrity or arbitrary natural-language request fidelity.",
            "Long or unfamiliar requests still use model synthesis, whose factual completeness remains a separate measured outcome. No live improvement or guaranteed speech completion is inferred.",
            "No production, test, frozen method, raw evidence or Git index was changed by this review."
        ]
    }
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
