#!/usr/bin/env python3
"""Serial local-model navigation development cases; no benchmark inputs or audio.

The real resolver, responder, coordinator, and simulated-map backend run together.
Raw model responses, repairs, all events, and backend attempts remain in JSONL.
Reported latencies start at final-text submission; they are not acoustic metrics.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.config import load_config, make_coordinator_config, make_llm
from agent.coordinator import events as E
from agent.coordinator.coordinator import Coordinator
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor
from agent.coordinator.ledger import Ledger
from agent.coordinator.llm_client import LLMClient
from agent.coordinator.resolver import IntentResolver
from agent.coordinator.responder import Responder
from agent.tools.car_tools import ROUTE_COMPUTE_S, build_car_manifest
from scripts.capture_run import command_output, git_state, loaded_model_receipts, validate_frozen_models, validate_local_config
from scripts.reliability_experiment import percentiles

# Declared before the first model run. Development cases, not held-out evidence.
CASES = {
    "correction_before_commit": {
        "turns": ["Navigate to the airport.", "Actually, go to the office instead."],
        "expected_effects": [["start_navigation", "P_OFFICE", []]], "final": "P_OFFICE",
        "correction_trigger": "synchronously on the first plan_ready event, before commitment",
    },
    "return_to_destination": {
        "turns": ["Navigate to the airport.", "Navigate to the office.", "Navigate to the airport again."],
        "expected_effects": [["start_navigation", p, []] for p in ["P_AIRPORT", "P_OFFICE", "P_AIRPORT"]],
        "final": "P_AIRPORT",
    },
    "waypoint_and_cancel": {
        "turns": ["Navigate to the office.", "Add Blue Tokai in Indiranagar as a stop.", "Cancel navigation."],
        "expected_effects": [["start_navigation", "P_OFFICE", []],
                             ["add_waypoint", "P_OFFICE", ["K_BLUETOKAI"]], ["cancel_navigation", None, []]],
        "final": None,
    },
    "ambiguous_then_explicit": {
        "turns": ["Navigate to Airport Road.", "I mean Starbucks, Airport Road. Navigate there."],
        "expected_effects": [["start_navigation", "K_SBUX_AIR", []]], "final": "K_SBUX_AIR",
        "first_turn": "no write attempts; response asks for clarification before explicit place name",
    },
}


class RecordedLLM(LLMClient):
    """Keep the production parser/repair behavior and record each raw response."""
    def __init__(self, base, record):
        self.cfg, self.client, self.record = base.cfg, base.client, record

    async def complete(self, system, user, json_stop=False):
        entry = {"system": system, "user": user, "json_stop": json_stop, "started": time.monotonic()}
        self.record("llm_request", **entry)
        try:
            raw = await super().complete(system, user, json_stop=json_stop)
        except BaseException as error:
            self.record("llm_incomplete", **entry, error=repr(error),
                        note="No full response returned; partial streamed text is unavailable here")
            raise
        self.record("llm_response", **entry, raw=raw, duration_ms=(time.monotonic() - entry["started"]) * 1000)
        return raw


def evaluate(name, run):
    expected = CASES[name]
    effects = [[e["tool"], e["result"]["destination_id"], e["result"]["stop_ids"]] for e in run["effects"]]
    checks = {"effect_sequence": effects == expected["expected_effects"],
              "final_destination": run["final_destination"] == expected["final"],
              "no_planner_or_background_error": not run["runtime_errors"],
              "finished": run["error"] is None}
    required_turns = {2} if name == "correction_before_commit" else set(range(1, len(expected["turns"]) + 1))
    response_turns = {r["turn"] for r in run["replies"] if r["kind"] in {"result", "response", "clarification"}}
    checks["required_turns_have_response_text"] = required_turns <= response_turns
    if name == "correction_before_commit":
        checks["no_superseded_dispatch"] = not any(a["turn"] == 1 for a in run["attempts"])
        checks["correction_injected"] = run["turns_submitted"] == 2
    if name == "ambiguous_then_explicit":
        checks["no_write_before_clarification"] = not any(a["turn"] == 1 and a["state_changing"] for a in run["attempts"])
        checks["clarification_text_present"] = any(
            r["turn"] == 1 and r["kind"] in {"result", "response", "clarification"}
            and ("?" in r["text"] or "please say" in r["text"].lower()) for r in run["replies"])
    return {"checks": checks, "success": all(checks.values()), "observed_effects": effects}


class Session:
    def __init__(self, name, base, cfg, trace):
        self.name, self.trace, self.turn, self.origins = name, trace, 0, {}
        self._origin_turn = ContextVar("navigation_origin_turn", default=None)
        self.attempts, self.effects, self.replies, self.runtime_errors = [], [], [], []
        self.active, self.events = None, []
        manifest, ledger, bus = build_car_manifest(), Ledger(), EventBus()
        for spec in manifest.tools.values():
            original, tool, changing = spec.fn, spec.name, spec.state_changing

            async def wrapped(_fn=original, _tool=tool, _changing=changing, **args):
                origin = self._origin_turn.get()
                attempt = {"tool": _tool, "args": args, "state_changing": _changing, "turn": origin,
                           "dispatch_ms": (time.monotonic() - self.origins[origin]) * 1000}
                self.attempts.append(attempt)
                self.record("backend_dispatch", **attempt)
                try:
                    result = await _fn(**args)
                except BaseException as error:
                    attempt.update(status="cancelled" if isinstance(error, asyncio.CancelledError) else "exception", error=repr(error))
                    self.record("backend_incomplete", **attempt)
                    raise
                attempt.update(status=result.get("status"), result=result,
                               completion_ms=(time.monotonic() - self.origins[attempt["turn"]]) * 1000)
                self.record("backend_result", **attempt)
                if _changing and result.get("status") == "success":
                    self.active = result.get("destination_id")
                    if not result.get("already_active") and not result.get("already_present"):
                        if _tool != "cancel_navigation" or result.get("cancelled") is not None:
                            self.effects.append(dict(attempt))
                return result

            spec.fn = wrapped
        llm = RecordedLLM(base, self.record)
        executor = Executor(manifest, ledger, bus, lambda tool, args, start, end: self.record(
            "executor_attempt_log", tool=tool, args=args, start=start, end=end))

        async def speak(text):
            self.record("text_delivery", text=text, note="No audio generated")

        config = make_coordinator_config(cfg)
        config.speak_results = True
        self.coordinator = Coordinator(IntentResolver(llm, manifest), executor, ledger, bus, speak, Responder(llm), config)
        bus.subscribe(self.event)

    def record(self, kind, **data):
        self.trace.write(json.dumps({"kind": kind, "at_monotonic": time.monotonic(),
                                    "turn": self._origin_turn.get(), "latest_turn": self.turn, **data}) + "\n")
        self.trace.flush()

    def event(self, event):
        self.events.append(event.to_dict())
        self.record("coordinator_event", event=event.to_dict())
        if event.type == E.AGENT_SAY:
            turn = event.data["intent_version"]
            self.replies.append({"turn": turn, "kind": event.data.get("kind"), "text": event.data["text"],
                                 "final_text_to_delivery_ms": (time.monotonic() - self.origins[turn]) * 1000})
        if event.type == E.TOOL_ERROR and event.data.get("tool") in {"resolver", "responder", "coordinator", "speech"}:
            self.runtime_errors.append(event.to_dict())
        if self.name == "correction_before_commit" and event.type == E.PLAN_READY and self.turn == 1:
            self.submit(CASES[self.name]["turns"][1])

    def submit(self, text):
        self.turn += 1
        self.origins[self.turn] = time.monotonic()
        # Spawned tasks inherit the submission's origin, even if a correction
        # arrives before they dispatch or their model response finishes.
        token = self._origin_turn.set(self.turn)
        try:
            self.record("final_text_submission", text=text)
            self.coordinator.on_user_turn(text)
        finally:
            self._origin_turn.reset(token)

    async def run(self, timeout):
        for text in CASES[self.name]["turns"][:1] if self.name == "correction_before_commit" else CASES[self.name]["turns"]:
            self.submit(text)
            await self.coordinator.drain(timeout=timeout)


async def experiment(args):
    cfg = load_config(args.profile)
    validate_local_config(cfg)
    validate_frozen_models(cfg)
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError("use a new output directory; prior experiments must be preserved")
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "experiment.json"
    report = {"kind": "real_local_llm_text_development", "status": "running",
              "created_at": datetime.now(timezone.utc).isoformat(), "configuration": cfg,
              "repository": git_state(ROOT), "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted((ROOT / "agent").rglob("*.py"))},
              "hardware": {"platform": platform.platform(), "cpu_count": os.cpu_count(), "python": sys.version,
                           "memory_bytes": command_output(["sysctl", "-n", "hw.memsize"]) if sys.platform == "darwin" else None},
              "python_packages": dict(sorted((d.metadata["Name"], d.version) for d in distributions() if d.metadata["Name"])),
              "model_load_receipts": loaded_model_receipts(), "expected_cases": CASES,
              "case_timeout_s": args.case_timeout, "route_compute_s": ROUTE_COMPUTE_S,
              "timing_origin": "final text submitted to coordinator, not speech end; no audio produced",
              "coordinator": {**asdict(make_coordinator_config(cfg)), "speak_results": True},
              "limitations": ["Independent development data, not held-out or Full-Duplex-Bench.",
                              "Loopback config/loader receipts do not independently attest server inference provenance.",
                              "No speech recognition, microphone, TTS, echo, or acoustic latency measurement.",
                              "Clarification text check is a heuristic; raw responses require semantic review.",
                              "Malformed model outputs are observed as errors; none are deliberately injected."], "runs": []}
    def save():
        path.write_text(json.dumps(report, indent=2) + "\n")
    save()  # Freeze cases and configuration on disk before the first request.
    base = make_llm(cfg)
    try:
        for name in args.cases or CASES:
            for repeat in range(1, args.repeats + 1):
                trace_name = f"{name}-{repeat}.jsonl"
                error = None
                with (args.out / trace_name).open("w") as trace:
                    session = Session(name, base, cfg, trace)
                    try:
                        await asyncio.wait_for(session.run(args.case_timeout), timeout=args.case_timeout)
                    except Exception as failure:
                        error = repr(failure)
                        session.record("case_error", error=error)
                    finally:
                        await session.coordinator.aclose(timeout=2)
                    run = {"case": name, "repeat": repeat, "trace": trace_name, "error": error,
                           "attempts": session.attempts, "effects": session.effects, "replies": session.replies,
                           "runtime_errors": session.runtime_errors, "turns_submitted": session.turn,
                           "final_destination": session.active}
                    run.update(evaluate(name, run))
                    report["runs"].append(run)
                save()
                print(json.dumps({"case": name, "repeat": repeat, "success": run["success"], "checks": run["checks"]}), flush=True)
    finally:
        await base.client.close()
    runs = report["runs"]
    first_responses = {}
    for i, run in enumerate(runs):
        for reply in run["replies"]:
            if reply["kind"] in {"result", "response", "clarification"}:
                first_responses.setdefault((i, reply["turn"]), reply["final_text_to_delivery_ms"])
    report.update(status="completed", summary={"runs": len(runs), "passed": sum(r["success"] for r in runs),
                  "backend_attempts": sum(len(r["attempts"]) for r in runs),
                  "backend_effects": sum(len(r["effects"]) for r in runs),
                  "runtime_errors": sum(len(r["runtime_errors"]) for r in runs),
                  "timeouts": sum(bool(r["error"] and "TimeoutError" in r["error"]) for r in runs),
                  "backend_errors": sum(a.get("status") in {"error", "failed", "exception"} for r in runs for a in r["attempts"]),
                  "final_text_to_first_response": percentiles(list(first_responses.values())),
                  "final_text_to_ack": percentiles([x["final_text_to_delivery_ms"] for r in runs for x in r["replies"] if x["kind"] == "ack"]),
                  "final_text_to_dispatch": percentiles([a["dispatch_ms"] for r in runs for a in r["attempts"]]),
                  "final_text_to_effect": percentiles([a["completion_ms"] for r in runs for a in r["effects"]])})
    save()
    return all(r["success"] for r in runs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", choices=["local-mac", "local-cuda"], default="local-mac")
    parser.add_argument("--case", dest="cases", choices=list(CASES), action="append")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--case-timeout", type=float, default=180)
    args = parser.parse_args()
    if args.repeats < 1 or args.case_timeout <= 0:
        parser.error("repeats and case-timeout must be positive")
    if args.cases and len(set(args.cases)) != len(args.cases):
        parser.error("each --case may appear only once")
    sys.exit(0 if asyncio.run(experiment(args)) else 1)
