"""Freeze independent simulated-navigation executions before summary measurement."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor, PlannedCall
from agent.coordinator.ledger import Ledger
from agent.tools import car_tools


def route(destination, via=None):
    args = {"destination": destination}
    if via:
        args["via"] = via
    return [PlannedCall("route", "compute_route", args),
            PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"})]


def serializable(ex):
    return {"calls": [asdict(c) for c in ex.calls],
            "outcomes": {k: asdict(v) for k, v in ex.outcomes.items()}, "stale": ex.stale}


async def main():
    # This is input construction only: no model, TTS or conversational timing.
    car_tools.ROUTE_COMPUTE_S = 0
    definitions = [
        ("start", "Navigate to MG Road Metro Station.", [],
         [PlannedCall("find", "search_destination", {"query": "MG Road Metro Station"}),
          PlannedCall("route", "compute_route", {"destination": "$find.places[0].place_id"}),
          PlannedCall("start", "start_navigation", {"route_id": "$route.route_id"})]),
        ("replace", "Actually, navigate to Whitefield instead.", route("Cubbon Park"), route("Whitefield")),
        ("via", "Navigate to Manipal Hospital via Blue Tokai, Indiranagar.", [],
         route("Manipal Hospital, Old Airport Road", "Blue Tokai, Indiranagar")),
        ("already_active", "Start the active route again.", route("office", "Ather Grid charger, Indiranagar"),
         [PlannedCall("start", "start_navigation", {"route_id": "R1"})]),
        ("add_stop", "Add the nearest coffee stop to the route.", route("MG Road Metro Station"),
         [PlannedCall("find", "find_nearby", {"category": "coffee"}),
          PlannedCall("stop", "add_waypoint", {"place_id": "$find.places[0].place_id"})]),
        ("repeat_stop", "Add Blue Tokai, Indiranagar as a stop again.", route("office", "Blue Tokai, Indiranagar"),
         [PlannedCall("stop", "add_waypoint", {"place_id": "Blue Tokai, Indiranagar"})]),
        ("cancel_active", "Cancel navigation.", route("Cubbon Park"), [PlannedCall("cancel", "cancel_navigation", {})]),
        ("cancel_idle", "Cancel navigation.", [], [PlannedCall("cancel", "cancel_navigation", {})]),
        ("route_only", "Compute a route to MG Road Metro Station without starting navigation.", [],
         [PlannedCall("route", "compute_route", {"destination": "MG Road Metro Station"})]),
        ("independent_query", "Navigate to MG Road Metro Station and find nearby coffee places.", [],
         [*route("MG Road Metro Station"), PlannedCall("find", "find_nearby", {"category": "coffee"})]),
    ]
    rows = []
    for name, request, setup, calls in definitions:
        bus = EventBus()
        executor = Executor(car_tools.build_car_manifest(), Ledger(), bus)
        try:
            before = await executor.run(setup) if setup else None
            result = await executor.run(calls)
            rows.append({"id": name, "request": request,
                         "expected_path": "model" if name in {"route_only", "independent_query"} else "direct",
                         "setup": serializable(before) if before else None, "execution": serializable(result),
                         "events": [e.to_dict() for e in bus.history]})
        finally:
            await executor.aclose()
    source = [*sorted((ROOT / "agent").rglob("*.py")), Path(__file__)]
    report = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "cases": rows,
              "construction": "Scripted real manifest/executor; simulated route delay zero only while constructing inputs. No inference.",
              "expected": "Preserve successful destination/stop/cancel facts, numeric ETA, replacement identity and no-op status. Preparatory routes must not be called active; independent query results must not be hidden by the direct path.",
              "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source}}
    with Path(__file__).with_name("inputs.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"cases": len(rows), "outcome_statuses": sorted({o['status'] for r in rows for o in r['execution']['outcomes'].values()})}))


if __name__ == "__main__":
    asyncio.run(main())
