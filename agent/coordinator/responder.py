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
say it is done. For "blocked" say it was already done earlier and was not repeated. For \
"error"/"skipped"/"cancelled" say it could not be completed. Reply with the sentence only."""


class Responder:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def summarize(self, request: str, ex: Execution) -> str:
        outcomes = [
            {"tool": o.tool, "args": o.args, "status": o.status, "result": o.result, "error": o.error}
            for o in ex.outcomes.values()
        ]
        if not outcomes:
            return ""
        user = f"User request: {request}\nResults: {json.dumps(outcomes, default=str)}"
        try:
            text = await self.llm.complete(SYSTEM, user)
        except Exception:
            return ""
        from .llm_client import _THINK
        return _THINK.sub("", text).strip().strip('"')
