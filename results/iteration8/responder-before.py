"""Responder: one short, grounded sentence about what the tools returned.

Only facts from tool results are allowed; "done" is only said for calls whose
status is done. Blocked duplicates are reported as already done.
"""

from __future__ import annotations

import json

from .executor import Execution
from .llm_client import LLMClient

SYSTEM = """You write what a voice assistant says after its tool calls finished. \
One or two short spoken sentences, max 30 words, no lists, no ids unless useful \
(booking reference is useful). Use ONLY facts in the results. For status "done" you may \
say it is done. For "blocked" say this operation was already completed and was not repeated. For \
"unknown" say the action's outcome could not be confirmed and it was not retried; never claim \
success or failure. For "error"/"skipped"/"cancelled" say it could not be completed. \
Reply with the sentence only."""


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
