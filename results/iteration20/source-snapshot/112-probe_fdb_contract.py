"""Probe pinned FDB scorer scope with invented tools, not benchmark items."""
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("v3", type=Path)
parser.add_argument("--out", type=Path, default=Path("prism-fdb-contract-probes.json"))
args = parser.parse_args()
sys.path.insert(0, str(args.v3.resolve()))
import evaluate_pass_rate as scorer

scenario = {"expected_tool_calls": [{"function": "route_to", "args": {"place": "Museum"}}]}
call = {"function": "route_to", "args": {"place": "Museum"}}
probes = {
    "correct_call_silent": scorer.evaluate_scenario_pass(scenario, [call], transcript="", result_data={"asr_chunks": []}),
    "correct_call_backend_error": scorer.evaluate_scenario_pass(scenario, [{**call, "result": {"status": "error"}}]),
    "extra_argument_ignored": scorer.evaluate_scenario_pass(scenario, [{"function": "route_to", "args": {"place": "Museum", "unexpected": "ignored"}}]),
    "dynamic_reference_unchecked": scorer.evaluate_scenario_pass({"expected_tool_calls": [{"function": "reserve", "args": {"id": "$RESULT_0.id"}}]}, [{"function": "reserve", "args": {"id": "invented-unrelated-id"}}]),
    "extra_call_fails": scorer.evaluate_scenario_pass(scenario, [call, call]),
    "wrong_literal_fails": scorer.evaluate_scenario_pass(scenario, [{"function": "route_to", "args": {"place": "Market"}}]),
}
output = {name: {"passed": result["passed"], "reason": result["failure_reason"]} for name, result in probes.items()}
args.out.write_text(json.dumps(output, indent=2) + "\n")
print(json.dumps(output, indent=2))
