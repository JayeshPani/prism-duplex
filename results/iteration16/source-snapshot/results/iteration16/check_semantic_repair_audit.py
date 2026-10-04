"""Invented event fixtures for receipt classification; no production/model imports."""
import importlib.util
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).with_name("audit_semantic_repair.py")
SPEC = importlib.util.spec_from_file_location("repair_audit", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def event(kind, **data):
    return {"type": kind, "data": data, "ts": 1.0}


def final(version=1):
    return event("user_final", intent_version=version, text="Navigate to Alder Gallery.")


def rejected(attempt=1):
    return event("plan_rejected", intent_version=1, attempt=attempt,
                 utterance="Navigate to Alder Gallery.", reason="Missing activation.",
                 rejected_plan={"complete": True, "calls": [{"id": "c1", "tool": "compute_route", "args": {"destination": "Alder Gallery"}}]})


def ready(fallback=False):
    return event("plan_ready", intent_version=1, utterance="Navigate to Alder Gallery.",
                 calls=[] if fallback else [{"id": "c1", "tool": "compute_route", "args": {}},
                                           {"id": "c2", "tool": "start_navigation", "args": {}}])


def started(call_id="c1", tool="compute_route"):
    return event("tool_started", operation_id="turn-1", execution_id="x", id=call_id, tool=tool, args={})


class RepairAuditFixtures(unittest.TestCase):
    def audit(self, events):
        return MODULE.audit_intents(events, 1)

    def test_initial_plan_is_one_parsed_receipt(self):
        result = self.audit([final(), ready(), started(), started("c2", "start_navigation")])
        self.assertTrue(result["checks"]["all_intent_invariants"])
        self.assertEqual(result["intents"][0]["status"], "initial_plan_accepted")
        self.assertEqual(result["summary"]["parsed_plan_receipts"], 1)
        self.assertEqual(result["summary"]["physical_tool_start_events"], 2)

    def test_rejected_plan_then_accepted_repair_with_same_call_id(self):
        result = self.audit([final(), rejected(), ready(), started(), started("c2", "start_navigation")])
        row = result["intents"][0]
        self.assertTrue(result["checks"]["all_intent_invariants"])
        self.assertEqual(row["status"], "repair_accepted")
        self.assertEqual(row["accepted_attempt_ordinal_from_control_flow"], 2)
        self.assertEqual(row["parsed_plan_receipts"], 2)
        self.assertIsNone(row["wire_model_request_count"])

    def test_exhaustion_keeps_fallback_out_of_parsed_count(self):
        result = self.audit([final(), rejected(), rejected(2),
            event("tool_error", intent_version=1, tool="resolver", error="Missing activation."), ready(True),
            event("gate_committed", intent_version=1, calls=[])])
        row = result["intents"][0]
        self.assertTrue(result["checks"]["all_intent_invariants"])
        self.assertEqual(row["status"], "repair_exhausted")
        self.assertEqual(row["parsed_plan_receipts"], 2)
        self.assertEqual(len(row["accepted_plan_rows"]), 0)
        self.assertEqual(len(row["fallback_plan_rows"]), 1)

    def test_parser_or_transport_error_does_not_invent_parsed_retry(self):
        result = self.audit([final(), rejected(), event("tool_error", intent_version=1,
                            tool="resolver", error="JSON error"), ready(True)])
        row = result["intents"][0]
        self.assertEqual(row["status"], "repair_failed_parser_or_other")
        self.assertEqual(row["parsed_plan_receipts"], 1)
        self.assertIsNone(row["parser_level_json_attempt_count"])

    def test_supersession_cannot_prove_retry_started(self):
        result = self.audit([final(), rejected(), final(2)])
        row = result["intents"][0]
        self.assertEqual(row["status"], "superseded_after_rejection")
        self.assertFalse(row["repair_start_receipt_available"])
        self.assertIsNone(row["actual_semantic_retry_started"])

    def test_rejected_only_or_early_dispatch_fails_even_if_later_accepted(self):
        result = self.audit([final(), rejected(), started(), ready()])
        self.assertFalse(result["checks"]["all_intent_invariants"])
        self.assertFalse(result["intents"][0]["checks"]["dispatch_only_after_accepted_plan"])
        result = self.audit([final(), rejected(), started()])
        self.assertFalse(result["intents"][0]["checks"]["no_dispatch_for_unaccepted_rejection"])

    def test_excess_retry_and_unknown_call_are_not_hidden(self):
        result = self.audit([final(), rejected(), rejected(2), rejected(3), ready(), started("rogue", "cancel_navigation")])
        checks = result["intents"][0]["checks"]
        self.assertFalse(checks["observed_rejection_attempt_order_bounded"])
        self.assertFalse(checks["no_acceptance_after_second_rejection"])
        self.assertFalse(checks["every_dispatch_call_in_accepted_plan"])

    def test_stale_acceptance_and_dispatch_fail(self):
        result = self.audit([final(), rejected(), final(2), ready(), started()])
        checks = result["intents"][0]["checks"]
        self.assertFalse(checks["no_acceptance_after_supersession"])
        self.assertFalse(checks["no_new_dispatch_after_supersession"])

    def test_unattributed_error_is_retained(self):
        result = self.audit([final(), rejected(), event("tool_error", tool="resolver", error="missing version")])
        self.assertFalse(result["checks"]["all_resolver_errors_attributed"])
        self.assertEqual(len(result["unattributed_resolver_errors"]), 1)

    def test_missing_captures_keep_all_declared_intents(self):
        with tempfile.TemporaryDirectory(dir=PATH.parent) as directory:
            base = Path(directory)
            (base / "protocol.json").write_text(json.dumps({
                "run_order": ["cancel", "repeat", "repeat", "cancel"],
                "expected": {"cancel": {"turns": [{}, {}]}, "repeat": {"turns": [{}, {}, {}]}}}))
            (base / "run-report.json").write_text(json.dumps({"finished_at": "fixture", "status": "failed"}))
            with patch.object(MODULE, "BASE", base), redirect_stdout(StringIO()):
                MODULE.main()
            result = json.loads((base / "semantic-repair-audit.json").read_text())
        self.assertEqual(result["summary"]["declared_intents"], 10)
        self.assertEqual(result["summary"]["declared_trials"], 4)
        self.assertEqual(result["summary"]["audited_trials"], 0)
        self.assertEqual(result["summary"]["parsed_plan_receipts"], 0)
        self.assertEqual(len(result["failed_checks"]), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
