#!/usr/bin/env python3
"""Independent planner-fidelity development probe; real local model, existing mocks.

No benchmark examples, held-out claims, result speech, or timing-performance claims.
Every complete model plan executes against mocks, even if it violates declared
checks. Incomplete plans do not dispatch, matching the production commit rule.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.config import load_config, make_llm
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.coordinator.resolver import IntentResolver
from agent.tools.bench_tools import build_bench_manifest
from agent.tools.car_tools import build_car_manifest
from scripts.capture_run import git_state, loaded_model_receipts, validate_frozen_models, validate_local_config
from scripts.local_navigation_experiment import RecordedLLM

# Independently declared before inference. Explicit budgets are USD; the mock
# catalog has no real stock/cart state. Expected IDs below belong to the demo map.
CASES = {
    "named_stop": {"domain": "car", "text": "Add Third Wave Coffee, Hebbal as a stop.", "kind": "named",
                   "target": "K_TWC_HEB", "names": ["Third Wave Coffee, Hebbal", "Third Wave Coffee", "Third Wave"],
                   "tools": ["add_waypoint"], "optional_lookup": "search_destination"},
    "category_no_cap": {"domain": "car", "text": "Find coffee stops along this route.", "kind": "category",
                        "tools": ["find_nearby"], "args": {"category": "coffee"}},
    "category_explicit_cap": {"domain": "car", "text": "Find coffee stops along this route with at most twelve minutes of detour.",
                              "kind": "category", "tools": ["find_nearby"], "args": {"category": "coffee", "max_detour_min": 12}},
    "product_no_budget": {"domain": "catalog", "text": "Search the catalog for cedar travel pouch.",
                          "kind": "product", "tools": ["search_products"], "budget": None},
    "product_explicit_budget": {"domain": "catalog", "text": "Search the catalog for cedar travel pouch under 65 dollars.",
                                "kind": "product", "tools": ["search_products"], "budget": 65},
    "search_and_add": {"domain": "catalog", "text": "Search for cedar travel pouch and add two of the first result to my cart.",
                       "kind": "cart", "tools": ["search_products", "add_to_cart"], "budget": None},
    "cancel_cart_action": {"domain": "catalog", "text": "Search for cedar travel pouch and add two to my cart. Actually, don't add anything; just search.",
                           "kind": "product", "tools": ["search_products"], "budget": None},
    "two_independent_actions": {"domain": "catalog", "text": "Search for cedar travel pouch and track order LM702.",
                                "kind": "search_track", "tools": ["search_products", "track_order"], "budget": None},
    "cancel_search_action": {"domain": "catalog", "text": "Search for cedar travel pouch and track order LM702. Actually, cancel the search; just track order LM702.",
                             "kind": "track", "tools": ["track_order"]},
}


def normalized(value):
    return " ".join(re.findall(r"\w+", str(value).lower()))


def named_match(value, case):
    name, names = normalized(value), case.get("names", [])
    words = name.split()
    connected = " ".join(w for i, w in enumerate(words) if not (0 < i < len(words) - 1 and w in {"in", "at"}))
    return bool(names) and (name in [normalized(x) for x in [case["target"], *names]] or connected == normalized(names[0]))


def assess(case, calls, outcomes, attempts, complete=True):
    """Separate semantic plan fidelity from observed mock output/effect checks."""
    tools = Counter(c.tool for c in calls)
    patterns = [Counter(case["tools"])]
    if case.get("optional_lookup"):
        patterns.append(Counter([*case["tools"], case["optional_lookup"]]))
    checks = {"complete": complete, "requested_tools_only": tools in patterns, "arguments_grounded": True,
              "dependencies_correct": True, "execution_succeeded": len(outcomes) == len(calls) and all(o.status in {"done", "reused"} for o in outcomes.values())}
    by_tool = {call.tool: call for call in calls}
    for call in calls:
        for key in {"query", "category", "order_id", "place_id", "product_id"} & call.args.keys():
            raw = call.args[key]
            checks["arguments_grounded"] &= raw is None or isinstance(raw, str)
            if key in {"query", "category", "order_id"}:
                checks["arguments_grounded"] &= not (isinstance(raw, str) and raw.startswith("$"))
        # Schema coercion happens in the executor; compare its resolved arguments
        # for literals, while retaining raw plan arguments for dependency checks.
        outcome = outcomes.get(call.id)
        args = {k: v for k, v in (outcome.args if outcome else call.args).items() if v is not None}
        if call.tool == "find_nearby":
            if args.get("category") == "cafe":
                args = {**args, "category": "coffee"}
            checks["arguments_grounded"] &= args == case.get("args")
        elif call.tool == "search_products":
            expected = {"query": "cedar travel pouch"}
            if case.get("budget") is not None:
                expected["max_price"] = case["budget"]
            checks["arguments_grounded"] &= {**args, "query": normalized(args.get("query"))} == expected
        elif call.tool == "track_order":
            checks["arguments_grounded"] &= args == {"order_id": "LM702"}
        elif call.tool in {"add_to_cart", "add_waypoint"}:
            allowed = {"product_id", "quantity"} if call.tool == "add_to_cart" else {"place_id"}
            checks["arguments_grounded"] &= set(args) == allowed
            if call.tool == "add_to_cart":
                checks["arguments_grounded"] &= args.get("quantity") == 2
            source = by_tool.get("search_products" if call.tool == "add_to_cart" else "search_destination")
            if source:
                key, path = ("product_id", "products[0].product_id") if call.tool == "add_to_cart" else ("place_id", "places[0].place_id")
                checks["dependencies_correct"] &= call.args.get(key) == f"${source.id}.{path}"
            elif call.tool == "add_waypoint":
                checks["arguments_grounded"] &= named_match(call.args.get("place_id"), case)
        elif call.tool == "search_destination":
            checks["arguments_grounded"] &= set(args) == {"query"} and named_match(call.args.get("query"), case)
    writes = [a for a in attempts if a["state_changing"]]
    if case["kind"] == "named":
        effects_ok = len(writes) == 1 and writes[0]["tool"] == "add_waypoint" and writes[0].get("result", {}).get("stop_ids") == [case["target"]] and writes[0].get("result", {}).get("destination_id") == "P_WHITEFIELD"
    elif case["kind"] == "cart":
        search = by_tool.get("search_products")
        products = (outcomes[search.id].result or {}).get("products", []) if search and search.id in outcomes else []
        result = writes[0].get("result", {}) if len(writes) == 1 else {}
        effects_ok = bool(products and len(writes) == 1 and writes[0]["tool"] == "add_to_cart" and result.get("product_id") == products[0]["product_id"] and result.get("quantity") == 2)
    else:
        effects_ok = not writes
    checks["observed_mock_effects"] = effects_ok
    return {"checks": checks, "plan_fidelity_passed": all(checks[k] for k in ("complete", "requested_tools_only", "arguments_grounded", "dependencies_correct")),
            "mock_execution_passed": checks["execution_succeeded"] and effects_ok, "passed": all(checks.values())}


async def run_case(name, case, base, trace):
    attempts, phase, ledger, bus = [], "setup", Ledger(), EventBus()
    manifest = build_car_manifest() if case["domain"] == "car" else build_bench_manifest("instant")
    def record(kind, **data):
        trace.write(json.dumps({"kind": kind, "phase": phase, "at_monotonic": time.monotonic(), **data}) + "\n")
        trace.flush()
    bus.subscribe(lambda event: record("event", event=event.to_dict()))
    for spec in manifest.tools.values():
        fn, tool, changing = spec.fn, spec.name, spec.state_changing
        async def wrapped(_fn=fn, _tool=tool, _changing=changing, **args):
            item = {"tool": _tool, "args": args, "state_changing": _changing, "phase": phase}
            attempts.append(item)
            record("backend_dispatch", **item)
            try:
                result = await _fn(**args)
                item.update(status=result.get("status"), result=result)
                return result
            except BaseException as error:
                item.update(status="cancelled" if isinstance(error, asyncio.CancelledError) else "error", error=repr(error))
                raise
            finally:
                record("backend_outcome", **item)
        spec.fn = wrapped
    executor = Executor(manifest, ledger, bus, lambda tool, args, start, end: record("attempt_log", tool=tool, args=args, start=start, end=end))
    result = {"case": name, "attempts": attempts, "error": None, "passed": False}
    try:
        history, slots = [], {}
        if case["domain"] == "car":
            setup = await executor.run([PlannedCall("route", "compute_route", {"destination": "Whitefield"}),
                                        PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"})], operation_id="declared-setup")
            if setup.outcomes["start"].status != "done":
                raise RuntimeError("declared navigation setup failed")
            snapshot = setup.outcomes["start"].result
            history = [{"role": "user", "text": "Navigate to Whitefield."},
                       {"role": "assistant", "text": "Navigation is active to Whitefield (ITPL)."}]
            slots = {"destination": "Whitefield"}
            record("explicit_setup_context", snapshot=snapshot, history=history, slots=slots)
        phase = "evaluation"
        record("input", text=case["text"], history=history, slots=slots, action_outcomes=ledger.summary())
        plan = await IntentResolver(RecordedLLM(base, record), manifest).resolve(case["text"], history, slots, ledger.summary())
        result["plan"] = asdict(plan)
        record("resolved_plan", plan=result["plan"])
        outcomes = {}
        if plan.complete:
            outcomes = (await executor.run(plan.calls, operation_id=name)).outcomes
        else:
            record("incomplete_plan_not_dispatched")
        result["outcomes"] = {key: asdict(value) for key, value in outcomes.items()}
        result.update(assess(case, plan.calls, outcomes, [a for a in attempts if a["phase"] == "evaluation"], plan.complete))
    except BaseException as error:
        result.update(error=repr(error), passed=False)
        record("case_error", error=repr(error))
        if isinstance(error, asyncio.CancelledError):
            raise
    finally:
        await executor.aclose(timeout=2)
        record("case_result", result=result)
    return result


async def experiment(args):
    cfg = load_config(args.profile)
    validate_local_config(cfg)
    validate_frozen_models(cfg)
    args.out.mkdir(parents=True, exist_ok=False)
    source_paths = [*sorted((ROOT / "agent").rglob("*.py")), ROOT / "bench/Full-Duplex-Bench/v3/mock_apis.py"]
    report = {"status": "running", "created_at": datetime.now(timezone.utc).isoformat(), "configuration": cfg,
              "runtime": {"python": sys.version, "platform": platform.platform()},
              "expected_cases": CASES, "case_sha256": hashlib.sha256(json.dumps(CASES, sort_keys=True).encode()).hexdigest(),
              "declared_setup": {"car": "Active Whitefield (P_WHITEFIELD) route from Home, empty stops; truthful history seeded from successful setup", "catalog": "Fresh existing mock registry per case"},
              "argument_policy": "Schema scalar coercion retained; absent/null optional arguments equivalent; query case/punctuation normalized; independent call order/IDs flexible; dependency reference paths exact",
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "repository": git_state(ROOT),
              "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths},
              "model_load_receipts": loaded_model_receipts(), "timeout_s": args.timeout,
              "limitations": ["Independent development cases, not held-out data or a benchmark score; no benchmark item data used",
                              "Existing benchmark mock implementations are reused as tools, not benchmark examples or expected answers",
                              "Catalog write outputs are stateless acknowledgments, not persistent cart effects",
                              "Configured local endpoint/loader receipts do not independently attest the model serving each response",
                              "No result speech, audio, live transport, interruption timing, or performance conclusions",
                              "Fixed serial order shares a warm server; model quality claims require broader separate evaluation"], "runs": []}
    def save():
        (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    save()  # Cases/configuration are on disk before the first model request.
    base = make_llm(cfg)
    try:
        for name, case in CASES.items():
            with (args.out / f"{name}.jsonl").open("w") as trace:
                try:
                    result = await asyncio.wait_for(run_case(name, case, base, trace), timeout=args.timeout)
                except TimeoutError:
                    result = {"case": name, "passed": False, "error": "TimeoutError; detailed attempts remain in trace"}
            report["runs"].append(result)
            save()
            print(json.dumps({"case": name, "passed": result["passed"], "checks": result.get("checks"), "error": result.get("error")}), flush=True)
    finally:
        await base.client.close()
    report.update(status="completed", summary={"cases": len(CASES), "passed": sum(r["passed"] for r in report["runs"]),
                  "plan_fidelity_passed": sum(r.get("plan_fidelity_passed", False) for r in report["runs"]),
                  "mock_execution_passed": sum(r.get("mock_execution_passed", False) for r in report["runs"])})
    save()
    return report["summary"]["passed"] == len(CASES)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", choices=("local-mac", "local-cuda"), default="local-mac")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    sys.exit(0 if asyncio.run(experiment(args)) else 1)
