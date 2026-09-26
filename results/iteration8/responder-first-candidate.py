"""Responder: one short, grounded sentence about what the tools returned.

Only facts from tool results are allowed; "done" is only said for calls whose
status is done. Blocked duplicates are reported as already done.
"""

from __future__ import annotations

import json

from .executor import Execution
from .llm_client import LLMClient
from .refs import deps_of

SYSTEM = """You write what a voice assistant says after its tool calls finished. \
One or two short spoken sentences, max 30 words, no lists, no ids unless useful \
(booking reference is useful). Use ONLY facts in the results. For status "done" you may \
say it is done. For "blocked" say this operation was already completed and was not repeated. For \
"unknown" say the action's outcome could not be confirmed and it was not retried; never claim \
success or failure. For "error"/"skipped"/"cancelled" say it could not be completed. \
Reply with the sentence only."""


def _navigation_summary(ex: Execution) -> str | None:
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
    # An independent query needs its own answer; don't hide it behind the action.
    needed = {target.id}
    while True:
        dependencies = {d for c in ex.calls if c.id in needed for d in deps_of(c.args)}
        if dependencies <= needed:
            break
        needed |= dependencies
    if needed != calls.keys():
        return None
    result = ex.outcomes[target.id].result
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
            text += f" ETA: {eta} minutes."
        else:
            added, present, extra = result.get("added"), result.get("already_present"), result.get("extra_min")
            if not isinstance(added, str) or not added or type(present) is not bool or type(extra) is not int:
                return None
            if present:
                text = f"The stop at {added} is already on your route. ETA to {destination}: {eta} minutes."
            else:
                text = f"Added {added} as a stop. ETA to {destination}: {eta} minutes, {extra} extra minutes."
    return text if len(text.split()) <= 30 else None


class Responder:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def summarize(self, request: str, ex: Execution) -> str:
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
        navigation = _navigation_summary(ex)
        if navigation is not None:
            return navigation
        outcomes = [
            {"tool": o.tool, "args": o.args, "status": o.status, "result": o.result, "error": o.error}
            for o in ex.outcomes.values()
        ]
        if not outcomes:
            return ""
        user = f"User request: {request}\nResults: {json.dumps(outcomes, default=str)}"
        text = await self.llm.complete(SYSTEM, user)
        from .llm_client import _THINK
        return _THINK.sub("", text).strip().strip('"')
