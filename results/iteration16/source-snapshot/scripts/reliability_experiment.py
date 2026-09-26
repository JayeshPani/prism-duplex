#!/usr/bin/env python3
"""Scripted coordinator experiment. No LLM, audio, network, or held-out items.

Use --repo to import another checkout with the same public coordinator/tool API.
Every backend attempt and coordinator event is retained, including failures and
cancellations. Timings start at final-transcript submission, not acoustic EOU.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import subprocess
import sys
import time
from pathlib import Path


SCENARIOS = {
    "correction_before_commit": {"destinations": ["P_OFFICE"], "final": "P_OFFICE"},
    "correction_during_route": {"destinations": ["P_OFFICE"], "final": "P_OFFICE"},
    # A write already dispatched cannot be undone by cancelling the task.
    "correction_during_write": {"destinations": ["P_AIRPORT", "P_OFFICE"], "final": "P_OFFICE"},
    "return_to_destination": {"destinations": ["P_AIRPORT", "P_OFFICE", "P_AIRPORT"], "final": "P_AIRPORT"},
    "overlapping_rooms": {"room_a": "P_AIRPORT", "room_b": "P_OFFICE"},
    "repeat_same_stop": {"destinations": ["P_OFFICE"], "final": "P_OFFICE", "polyline_points": 3},
    "read_lookup": {"place_id": "K_BLUETOKAI"},
}
POLICIES = {"fixed": {"hold_read_ms": 40, "hold_state_ms": 40},
            "read_write_adaptive": {"hold_read_ms": 20, "hold_state_ms": 60}}


def percentiles(values):
    if not values:
        return {"n": 0, "p50_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {"n": len(values), **{f"p{p}_ms": round(ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)], 3)
                                for p in (50, 95)}}


def revision(repo):
    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return None


async def experiment(args):
    # Import after selecting the target checkout; this permits an honest rerun
    # of the same scenarios against the unchanged baseline implementation.
    sys.path.insert(0, str(args.repo))
    from agent.coordinator.coordinator import Coordinator, CoordinatorConfig
    from agent.coordinator.events import EventBus
    from agent.coordinator.executor import Executor, PlannedCall
    from agent.coordinator.ledger import Ledger
    from agent.coordinator.resolver import Resolution
    from agent.tools import car_tools

    car_tools.ROUTE_COMPUTE_S = 0.040
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"kind": "scripted_development_experiment", "status": "running", "repo": str(args.repo),
              "commit": args.revision or revision(args.repo), "python": sys.version, "platform": platform.platform(),
              "machine": platform.machine(), "repeats": args.repeats, "policies": POLICIES,
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "source_sha256": {str(path.relative_to(args.repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sorted((args.repo / "agent").rglob("*.py"))},
              "route_compute_ms": 40, "write_delay_ms_only_in_write_boundary_scenario": 30,
              "expected_outcomes_declared_before_execution": SCENARIOS,
              "timing_origin": "final transcript submission (not speech end or acoustic playback)",
              "limitations": ["Scripted resolver and simulated map; no model or speech-quality measurement.",
                              "Scaled timing illustrates tradeoffs; it does not tune production thresholds.",
                              "No held-out evaluation or proof of crash-safe exactly-once execution."],
              "intended_action_count_definition": "Each requested navigation, distinct stop, or lookup; superseded destinations excluded. Required actions count even in scenarios failing for extra effects.",
              "runs": []}
    report_path = args.out / "experiment.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")

    class Resolver:
        async def resolve(self, utterance, *unused):
            if "Blue Tokai again" in utterance:
                calls = [PlannedCall("stop", "add_waypoint", {"place_id": "Blue Tokai, Indiranagar"})]
            elif "add Blue Tokai" in utterance:
                calls = [PlannedCall("stop", "add_waypoint", {"place_id": "K_BLUETOKAI"})]
            elif "find Blue Tokai" in utterance:
                calls = [PlannedCall("lookup", "search_destination", {"query": "Blue Tokai"})]
            else:
                destination = max((utterance.rfind(name), name) for name in ("airport", "office"))[1]
                calls = [PlannedCall("route", "compute_route", {"destination": destination}),
                         PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"})]
            return Resolution(True, utterance, [], {}, calls, "")

    class Room:
        def __init__(self, name, policy, trace, write_delay=0):
            self.name, self.trace, self.write_delay = name, trace, write_delay
            self.turn, self.target, self.active, self.polyline = 0, None, None, []
            self.finals, self.routes, self.attempts, self.destinations = {}, {}, [], []
            self.lookup_result = None
            self.started = {name: asyncio.Event() for name in ("compute_route", "start_navigation")}
            manifest, bus, ledger = car_tools.build_car_manifest(), EventBus(), Ledger()
            bus.subscribe(lambda event: self.record("coordinator_event", event=event.to_dict()))
            for spec in manifest.tools.values():
                original, tool = spec.fn, spec.name

                async def wrapped(_original=original, _tool=tool, **kw):
                    destination = None
                    if _tool == "compute_route":
                        destination = {"airport": "P_AIRPORT", "office": "P_OFFICE"}.get(kw["destination"])
                    elif _tool == "start_navigation":
                        destination = self.routes.get(kw["route_id"], {}).get("destination_id")
                    stale = destination is not None and destination != self.target
                    origin = self.turn
                    if stale and _tool == "start_navigation":
                        origin = self.routes[kw["route_id"]]["source_turn"]
                    attempt = {"tool": _tool, "args": kw, "source_turn": origin,
                               "destination_id": destination, "stale_at_dispatch": stale,
                               "started_monotonic": time.monotonic()}
                    attempt["final_transcript_to_dispatch_ms"] = (attempt["started_monotonic"] - self.finals[origin]) * 1000
                    self.attempts.append(attempt)
                    self.record("backend_dispatch", attempt=dict(attempt))
                    if _tool in self.started:
                        self.started[_tool].set()
                    try:
                        if _tool == "start_navigation" and self.write_delay:
                            await asyncio.sleep(self.write_delay)
                        result = await _original(**kw)
                    except asyncio.CancelledError:
                        attempt["status"] = "cancelled"
                        self.record("backend_cancelled", attempt=dict(attempt))
                        raise
                    except Exception as error:
                        attempt.update(status="exception", error=repr(error))
                        self.record("backend_exception", attempt=dict(attempt))
                        raise
                    attempt.update(status=result.get("status", "success"), result=result,
                                   final_transcript_to_completion_ms=(time.monotonic() - self.finals[origin]) * 1000,
                                   superseded_after_dispatch=destination is not None and destination != self.target and not stale)
                    if result.get("status") == "success":
                        if _tool == "compute_route":
                            self.routes[result["route_id"]] = {**result, "source_turn": origin}
                        elif _tool == "start_navigation":
                            self.active, self.polyline = destination, result["polyline"]
                            if not result.get("already_active"):
                                self.destinations.append(destination)
                        elif _tool == "add_waypoint":
                            self.polyline = result["polyline"]
                        elif _tool == "search_destination":
                            self.lookup_result = result
                    self.record("backend_result", attempt=dict(attempt))
                    return result

                spec.fn = wrapped

            async def speak(text):
                self.record("speech_callback", text=text, note="No audio was produced")

            def logged(tool, kw, t0, t1):
                self.record("executor_tool_log", tool=tool, args=kw, start=t0, end=t1)

            executor = Executor(manifest, ledger, bus, logged)
            config = CoordinatorConfig(**POLICIES[policy], hold_incomplete_ms=100, ack_after_ms=10_000,
                                       resume_grace_ms=20, speak_results=False)
            self.coordinator = Coordinator(Resolver(), executor, ledger, bus, speak, config=config)

        def record(self, kind, **data):
            self.trace.write(json.dumps({"kind": kind, "room": self.name, "monotonic": time.monotonic(), **data}) + "\n")

        def say(self, text):
            self.turn += 1
            self.finals[self.turn] = time.monotonic()
            if "airport" in text or "office" in text:
                self.target = "P_OFFICE" if text.rfind("office") > text.rfind("airport") else "P_AIRPORT"
            self.record("transcript_submission", turn=self.turn, text=text, desired_destination=self.target)
            self.coordinator.on_user_turn(text)

        async def drain(self):
            # Old drain silently times out; the outer timeout detects this too.
            await asyncio.wait_for(self.coordinator.drain(timeout=5), timeout=2)

        async def close(self):
            if hasattr(self.coordinator, "aclose"):
                await self.coordinator.aclose(timeout=1)

    for policy in POLICIES:
        for name, expected in SCENARIOS.items():
            for repeat in range(args.repeats):
                trace_path = args.out / f"trace-{policy}-{name}-{repeat + 1}.jsonl"
                result = {"policy": policy, "scenario": name, "repeat": repeat + 1, "trace": trace_path.name,
                          "timeout": False, "error": None}
                rooms = []
                with trace_path.open("w") as trace:
                    room = Room("a", policy, trace, write_delay=0.030 if name == "correction_during_write" else 0)
                    rooms.append(room)
                    try:
                        if name == "read_lookup":
                            room.say("find Blue Tokai")
                        elif name == "overlapping_rooms":
                            other = Room("b", policy, trace)
                            rooms.append(other)
                            room.say("navigate to airport")
                            other.say("navigate to office")
                        else:
                            room.say("navigate to office" if name == "repeat_same_stop" else "navigate to airport")
                            if name == "correction_before_commit":
                                await asyncio.sleep(0.010)
                                room.coordinator.on_user_speaking()
                                room.say("actually office")
                            elif name in ("correction_during_route", "correction_during_write"):
                                tool = "compute_route" if name == "correction_during_route" else "start_navigation"
                                await asyncio.wait_for(room.started[tool].wait(), timeout=1)
                                if name == "correction_during_route":
                                    await asyncio.sleep(0.025)
                                room.say("actually office")
                            elif name == "return_to_destination":
                                for text in ("navigate to office", "navigate to airport"):
                                    await room.drain()
                                    room.say(text)
                            elif name == "repeat_same_stop":
                                for text in ("add Blue Tokai", "add Blue Tokai again"):
                                    await room.drain()
                                    room.say(text)
                        await asyncio.gather(*(r.drain() for r in rooms))
                    except TimeoutError:
                        result["timeout"] = True
                    except Exception as error:
                        result["error"] = repr(error)
                    finally:
                        for item in rooms:
                            await item.close()

                attempts = [a for r in rooms for a in r.attempts]
                if name == "overlapping_rooms":
                    correct = len(rooms) == 2 and room.active == expected["room_a"] and rooms[1].active == expected["room_b"]
                    intended_expected = 2
                    intended_completed = sum(r.active == expected[f"room_{r.name}"] for r in rooms)
                elif name == "read_lookup":
                    correct = bool(room.lookup_result and room.lookup_result["places"][0]["place_id"] == expected["place_id"])
                    intended_expected, intended_completed = 1, int(correct)
                else:
                    correct = room.active == expected["final"] and room.destinations == expected["destinations"]
                    required_destinations = ["P_OFFICE"] if name == "correction_during_write" else expected["destinations"]
                    intended_expected, intended_completed = len(required_destinations), 0
                    observed = iter(room.destinations)
                    for destination in required_destinations:
                        if any(actual == destination for actual in observed):
                            intended_completed += 1
                    if "polyline_points" in expected:
                        stop_correct = len(room.polyline) == expected["polyline_points"]
                        correct = correct and stop_correct
                        intended_expected += 1
                        intended_completed += int(stop_correct)
                result.update(success=correct and not result["timeout"] and result["error"] is None,
                              intended_actions_expected=intended_expected, intended_actions_completed=intended_completed,
                              stale_dispatches=sum(a["stale_at_dispatch"] for a in attempts),
                              superseded_inflight_write_completions=sum(a.get("superseded_after_dispatch", False)
                                                                       and a["tool"] in ("start_navigation", "add_waypoint")
                                                                       for a in attempts),
                              backend_attempts=len(attempts), cancelled_attempts=sum(a.get("status") == "cancelled" for a in attempts),
                              observed_destinations={r.name: r.destinations for r in rooms},
                              final_destinations={r.name: r.active for r in rooms},
                              final_transcript_to_dispatch_ms=[a["final_transcript_to_dispatch_ms"] for a in attempts],
                              final_transcript_to_tool_completion_ms=[a["final_transcript_to_completion_ms"] for a in attempts
                                                                     if a.get("status") == "success"],
                              final_transcript_to_effect_completion_ms=[a["final_transcript_to_completion_ms"] for a in attempts
                                                                       if a.get("status") == "success" and
                                                                       a["tool"] in ("start_navigation", "add_waypoint") and
                                                                       not a["result"].get("already_active") and
                                                                       not a["result"].get("already_present")])
                report["runs"].append(result)

    report["status"] = "completed"
    report["summary"] = {}
    for policy in POLICIES:
        runs = [r for r in report["runs"] if r["policy"] == policy]
        report["summary"][policy] = {"runs": len(runs), "successful_scenarios": sum(r["success"] for r in runs),
                                     "intended_actions_expected": sum(r["intended_actions_expected"] for r in runs),
                                     "intended_actions_completed": sum(r["intended_actions_completed"] for r in runs),
                                     "timeouts": sum(r["timeout"] for r in runs),
                                     "stale_dispatches": sum(r["stale_dispatches"] for r in runs),
                                     "superseded_inflight_write_completions": sum(r["superseded_inflight_write_completions"] for r in runs),
                                     **{metric: percentiles([value for r in runs for value in r[metric]]) for metric in
                                        ("final_transcript_to_dispatch_ms", "final_transcript_to_tool_completion_ms",
                                         "final_transcript_to_effect_completion_ms")}}
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, default=Path("results/reliability"))
    parser.add_argument("--revision", help="Explicit verified source commit for a git archive without repository metadata")
    parser.add_argument("--repeats", type=int, default=5)
    arguments = parser.parse_args()
    if arguments.repeats < 1:
        parser.error("--repeats must be positive")
    arguments.repo = arguments.repo.resolve()
    asyncio.run(experiment(arguments))
