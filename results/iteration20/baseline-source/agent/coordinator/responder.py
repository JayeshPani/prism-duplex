"""Responder: one short, grounded sentence about what the tools returned.

Only facts from tool results are allowed; "done" is only said for calls whose
status is done. Blocked duplicates are reported as already done.
"""

from __future__ import annotations

import json
import re

from .executor import Execution
from .llm_client import LLMClient
from .refs import deps_of, is_ref

SYSTEM = """You write what a voice assistant says after its tool calls finished. \
One or two short spoken sentences, max 30 words, no lists, no ids unless useful \
(booking reference is useful). Use ONLY facts in the results. For status "done" you may \
say it is done. For "blocked" say this operation was already completed and was not repeated. For \
"unknown" say the action's outcome could not be confirmed and it was not retried; never claim \
success or failure. For "error"/"skipped"/"cancelled" say it could not be completed. \
Answer the original user request after applying its corrections. A repaired request is \
an interpretation; do not drop questions or requested details absent from that interpretation. \
Reply with the sentence only."""


def _same_destination_search(call, result: dict, destination_id: str | None) -> bool:
    query = call.args.get("query")
    places = result.get("places")
    return (call.tool == "search_destination"
            and isinstance(query, str) and bool(query.strip()) and not is_ref(query)
            and isinstance(destination_id, str) and bool(destination_id.strip())
            and isinstance(places, list) and len(places) == 1
            and isinstance(places[0], dict) and places[0].get("place_id") == destination_id
            and isinstance(places[0].get("name"), str) and bool(places[0]["name"].strip()))


def _action_only_request(request: str, ex: Execution, target) -> bool:
    """Recognize complete action commands; unfamiliar wording keeps model synthesis.

    A dependency can also be requested information, so graph shape alone cannot
    establish that an action confirmation answers the user's whole request.
    """
    normalize = lambda value: " ".join(re.findall(r"\w+", value.casefold()))
    command = re.sub(r"^(?:please |actually )+", "", normalize(request))
    result = ex.outcomes[target.id].result
    if target.tool == "cancel_navigation":
        return command in {"cancel navigation", "stop navigation", "cancel the navigation", "stop the navigation"}
    if target.tool == "start_navigation" and command in {
        "start navigation", "start the active route again", "start this route again",
    }:
        return True
    if target.tool == "add_waypoint" and command in {"add that stop again", "add that same stop again"}:
        return True

    names = {result.get("destination") if target.tool == "start_navigation" else result.get("added")}
    for call in ex.calls:
        if call.tool == "compute_route" and target.tool == "start_navigation":
            value = call.args.get("destination")
            destination_id = result.get("destination_id")
            if (isinstance(destination_id, str) and destination_id
                    and ex.outcomes[call.id].result.get("destination_id") == destination_id
                    and isinstance(value, str) and not is_ref(value)):
                names.add(value)
        elif call.tool == "search_destination":
            found = ex.outcomes[call.id].result
            if target.tool == "start_navigation":
                if _same_destination_search(call, found, result.get("destination_id")):
                    names.add(call.args["query"])
            elif any(p.get("name") in names for p in found.get("places", [])):
                names.add(call.args.get("query"))
        elif call.id == target.id and target.tool == "add_waypoint":
            value = call.args.get("place_id")
            if isinstance(value, str) and not is_ref(value):
                names.add(value)
    names = {normalize(variant) for name in names if isinstance(name, str) and name
             for variant in (name, name.replace(",", " in"), name.replace(",", " at"))}
    label = "(?:" + "|".join(re.escape(n) for n in names) + ")"
    if target.tool == "start_navigation":
        stops = result.get("stops", [])
        via = ""
        if stops and all(isinstance(s, str) for s in stops):
            via = "(?: via " + re.escape(normalize(" and ".join(stops))) + ")?"
        pattern = r"(?:navigate to|go to|drive to|take me to|head to|start navigation to|change the destination to) (?:the )?" + label + via + r"(?: instead| again)?"
    else:
        for call in ex.calls:
            if call.tool == "find_nearby" and isinstance(call.args.get("category"), str):
                category = normalize(call.args["category"])
                if command in {f"add the nearest {category} stop to the route", f"add the nearest {category} as a stop"}:
                    return True
        pattern = r"add (?:the )?" + label + r"(?: as a stop)?(?: again)?"
    return re.fullmatch(pattern, command) is not None


def _navigation_summary(request: str, ex: Execution) -> str | None:
    """Speak one confirmed navigation action and its prerequisite reads directly."""
    mutations = {"start_navigation", "add_waypoint", "cancel_navigation"}
    reads = {"search_destination", "compute_route", "find_nearby"}
    calls = {c.id: c for c in ex.calls}
    targets = [c for c in ex.calls if c.tool in mutations]
    if ex.stale or len(targets) != 1 or calls.keys() != ex.outcomes.keys():
        return None
    target = targets[0]
    for outcome in ex.outcomes.values():
        allowed = {"done"} if outcome.id == target.id else {"done", "reused"}
        if (outcome.tool not in mutations | reads or outcome.status not in allowed
                or not outcome.result or outcome.result.get("status") != "success"):
            return None
    result = ex.outcomes[target.id].result
    # Preserve independent queries, except a redundant lookup of the confirmed
    # destination. The whole-request guard still rejects requested details.
    needed = {target.id}
    while True:
        dependencies = {d for c in ex.calls if c.id in needed for d in deps_of(c.args)}
        if dependencies <= needed:
            break
        needed |= dependencies
    if needed != calls.keys():
        if (target.tool != "start_navigation" or needed - calls.keys()
                or any(not _same_destination_search(calls[cid], ex.outcomes[cid].result,
                                                    result.get("destination_id"))
                       for cid in calls.keys() - needed)):
            return None
    if target.tool == "cancel_navigation":
        if result.get("route_active") is not False or "cancelled" not in result:
            return None
        cancelled = result["cancelled"]
        if cancelled is None:
            text = "There is no active navigation to cancel."
        elif isinstance(cancelled, str) and cancelled:
            text = f"Navigation to {cancelled} has been cancelled."
        else:
            return None
    else:
        destination, eta = result.get("destination"), result.get("eta_min")
        if (result.get("route_active") is not True or not isinstance(destination, str)
                or not destination or type(eta) is not int or eta < 1):
            return None
        if target.tool == "start_navigation":
            active, stops, replaced = result.get("already_active"), result.get("stops"), result.get("replaced")
            if (type(active) is not bool or result.get("navigating_to") != destination
                    or not isinstance(stops, list) or any(not isinstance(s, str) or not s for s in stops)
                    or (replaced is not None and not isinstance(replaced, str))):
                return None
            via = f" via {', '.join(stops)}" if stops else ""
            if active:
                text = f"Navigation to {destination}{via} is already active."
            elif replaced and replaced != destination:
                text = f"Now navigating to {destination}{via}, replacing {replaced}."
            else:
                text = f"Navigation to {destination}{via} has started."
            text += f" Estimated arrival in {eta} minutes."
        else:
            added, present, extra = result.get("added"), result.get("already_present"), result.get("extra_min")
            if not isinstance(added, str) or not added or type(present) is not bool or type(extra) is not int:
                return None
            if present:
                text = f"The stop at {added} is already on your route. Estimated time to {destination} is {eta} minutes."
            else:
                text = f"Added {added} as a stop. Estimated time to {destination} is {eta} minutes, {extra} extra minutes."
    return text if len(text.split()) <= 30 and _action_only_request(request, ex, target) else None


class Responder:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def summarize(self, request: str, ex: Execution, *, repaired_request: str | None = None,
                        continued_request: str | None = None) -> str:
        if any(o.status == "unknown" for o in ex.outcomes.values()):
            return "I couldn't confirm whether that action completed, so I haven't retried it."
        for outcome in ex.outcomes.values():
            result = outcome.result or {}
            if outcome.status != "error" or result.get("code") not in ("place_ambiguous", "place_not_found"):
                continue
            names = [p["name"] for p in result.get("candidates", [])]
            if result["code"] == "place_ambiguous":
                if len(names) == 2:
                    return f"Which place do you mean: {names[0]} or {names[1]}?"
                return f"Which place do you mean? Please say its full name, such as {names[0]}."
            if names:
                return f"I couldn't find that place. Please say its full name, such as {names[0]}."
            return "I couldn't find that place on this map. Please say its name again."
        # A continuation is grounded by the manifest from the actual prior tool
        # error and literal answer. Model-written repairs are not such evidence.
        navigation = _navigation_summary(continued_request or request, ex)
        if navigation is not None:
            return navigation
        outcomes = [
            {"tool": o.tool, "args": o.args, "status": o.status, "result": o.result, "error": o.error}
            for o in ex.outcomes.values()
        ]
        if not outcomes:
            return ""
        user = f"User request: {request}"
        if repaired_request:
            user += f"\nRepaired request (interpretation): {repaired_request}"
        if continued_request:
            user += f"\nGrounded continued request: {continued_request}"
        user += f"\nResults: {json.dumps(outcomes, default=str)}"
        text = await self.llm.complete(SYSTEM, user)
        from .llm_client import _THINK
        return _THINK.sub("", text).strip().strip('"')
