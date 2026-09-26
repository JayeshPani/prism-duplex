"""IntentResolver: disfluent speech -> repaired request + slots + tool plan.

One initial plan request per (merged) utterance; the coordinator may request one
domain-contract repair. The client's JSON parsing retry is separate. The prompt
encodes general disfluency/self-repair rules; its illustrative examples use
entities that do not come from any benchmark item.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .executor import PlannedCall
from .llm_client import LLMClient
from .normalize import planner_hints
from ..tools.manifest import Manifest

SYSTEM_TEMPLATE = """You are the planning brain of a real-time voice assistant. You receive the \
user's latest spoken utterance as raw speech-to-text: it may contain fillers (um, uh), pauses \
(...), hesitations, false starts, repetitions and self-corrections. You decide what the user \
FINALLY wants and which tool calls to run. You never talk to the user directly except through \
the "reply" field.

TOOLS
Arguments marked ? are optional. Omit optional filters or limits the user did not supply \
for the active request; do not invent values to fill them. Tool-documented defaults and \
grounded identifiers/references needed to connect requested actions remain valid.
{tools}

REPAIR RULES
1. The LAST stated value wins. "X, actually no, Y", "X - scratch that - Y", "X, I mean Y", \
"make that Y", "not X, Y", "wait, Y instead" => use Y. Never call a tool with X.
2. A correction only replaces the slot it talks about. "make it Friday" changes the date and \
keeps the destination.
3. Ignore fillers, stutters and abandoned false starts ("I want to b- book..." = book).
4. If the user cancels a request ("never mind the second one", "forget it"), drop those calls.
5. Spelled or chunked ids/numbers are joined: "A-B-4-4" -> "AB44", "one two three" -> "123", \
"double seven" -> "77". Keep letters uppercase. Numbers are digits, amounts without currency \
symbols.
6. Dates/times: keep them as the user said them, normalized lightly ("the 5th of June" -> \
"June 5"). Do not invent a year.
7. Currencies: 3-letter ISO codes (dollars -> USD, euros -> EUR, yen -> JPY, pounds -> GBP, \
rupees -> INR).
8. Free-text arguments (search queries, names, places) use the user's own words: keep plural or \
singular, brand and model names exactly as spoken; only drop fillers.

PLANNING RULES
9. Call a tool only for things the user actually asked for in THIS utterance (or a pending \
request from earlier that now has all its details). Do not add "helpful" extra calls, do not \
repeat calls already listed under ACTION OUTCOMES unless the user explicitly asks again or \
changes a detail. Remarks that only acknowledge or restate an earlier request ("that would be \
great", "yes please", "sounds good") need no new calls. An "unknown" outcome is NOT
a completed action: its effect is unconfirmed; do not repeat it without backend reconciliation.
10. Multi-step requests become a chain. A later call can use a field of an earlier result with \
a reference string "$<call id>.<path>", e.g. "$c1.flights[0].flight_id". Use the result shapes \
shown after "->" in the tool list.
For a complete request with all required details, include every independently requested \
action after applying corrections and cancellations, not only the first clause. Include \
necessary prerequisite calls, but no additional actions.
11. Greetings, small talk, or requests with a missing required detail: no calls; reply briefly \
(ask for the missing detail if needed).
12. If the utterance looks cut off mid-thought (ends with "and", "to", "the", "uh" or an \
incomplete request), set "complete": false but still plan your best reading of it - the system \
waits a little longer for the user to continue before acting.
13. "reply" is ONE short spoken sentence (max ~20 words) describing what you are doing now, \
using the corrected values, e.g. "Searching flights to Oslo on May 3rd." Never claim an action \
is finished - results are not known yet. Do not read out ids unless the user gave them.

OUTPUT: a single JSON object, no prose, keys in this order:
{{"complete": true|false,
  "corrections": [{{"slot": "...", "from": "<dropped value>", "to": "<kept value>"}}],
  "calls": [{{"id": "c1", "tool": "<name>", "args": {{...}}}}],
  "reply": "<one short sentence>"}}

EXAMPLES (illustrative, not related to any real request)
User: "Can you uh... check the weather in Lyon for, for Tuesday - no wait, Wednesday."
-> {{"complete": true, "corrections": [{{"slot": "day", "from": "Tuesday", "to": "Wednesday"}}], "calls": [{{"id": "c1", "tool": "get_weather", "args": {{"city": "Lyon", "day": "Wednesday"}}}}], "reply": "Checking the weather in Lyon for Wednesday."}}
User: "Find me a, um, a desk lamp under forty bucks and then put two of them in my basket."
-> {{"complete": true, "corrections": [], "calls": [{{"id": "c1", "tool": "find_item", "args": {{"query": "desk lamp", "max_price": 40}}}}, {{"id": "c2", "tool": "add_item", "args": {{"item_id": "$c1.items[0].item_id", "quantity": 2}}}}], "reply": "Looking for a desk lamp under forty and adding two to your basket."}}
User: "Hey there, hope you're doing well."
-> {{"complete": true, "corrections": [], "calls": [], "reply": "Hi! What can I help you with?"}}
User: "Book me a haircut for, um, Saturday at..."
-> {{"complete": false, "corrections": [], "calls": [], "reply": "What time on Saturday?"}}
(The example tools above do not exist here; only use the TOOLS listed.)
{domain_notes}"""


_PLACEHOLDERS = {"", "unknown", "none", "n/a", "na", "null", "something", "tbd", "?", "not provided",
                 "not specified", "unspecified", "your order", "order id", "product id", "the item"}


def _placeholder(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower().strip("<>[]'\" ") in _PLACEHOLDERS)


@dataclass
class Resolution:
    complete: bool
    repaired: str
    corrections: list[dict[str, Any]]
    slots: dict[str, Any]
    calls: list[PlannedCall]
    reply: str
    raw: dict[str, Any] = field(default_factory=dict)


class PlanContractError(ValueError):
    """A parsed, schema-valid plan violates a domain's narrow request contract."""
    def __init__(self, reason: str, rejected_plan: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rejected_plan = deepcopy(rejected_plan)


class IntentResolver:
    def __init__(self, llm: LLMClient, manifest: Manifest) -> None:
        self.llm = llm
        self.manifest = manifest
        self.system = SYSTEM_TEMPLATE.format(
            tools=manifest.prompt_block(),
            domain_notes=("\nDOMAIN NOTES\n" + manifest.domain_notes) if manifest.domain_notes else "",
        )

    def build_user_prompt(self, utterance: str, history: list[dict[str, str]],
                          slots: dict[str, Any], completed: list[dict[str, Any]], *,
                          continuation: dict[str, str] | None = None) -> str:
        parts = []
        if history:
            parts.append("CONVERSATION SO FAR\n" + "\n".join(f"{h['role']}: {h['text']}" for h in history[-8:]))
        if slots:
            parts.append("CURRENT SLOTS\n" + json.dumps(slots, ensure_ascii=False))
        if completed:
            parts.append("ACTION OUTCOMES (check status; unknown means unconfirmed)\n"
                         + "\n".join(json.dumps(c, ensure_ascii=False) for c in completed[-10:]))
        parts.append(f'LATEST USER UTTERANCE\n"{utterance}"')
        if continuation:
            parts.append("GROUNDED REQUEST CONTINUATION\n" + json.dumps(continuation, ensure_ascii=False)
                         + "\nThe latest utterance supplies the missing destination for this original request. "
                           "Plan every action in the whole continued request, not only a lookup of the answer.")
        hints = planner_hints(utterance)
        if hints:
            parts.append(hints)
        parts.append("Return the JSON object.")
        return "\n\n".join(parts)

    async def resolve(self, utterance: str, history: list[dict[str, str]],
                      slots: dict[str, Any], completed: list[dict[str, Any]], *,
                      continuation: dict[str, str] | None = None) -> Resolution:
        data = await self.llm.complete_json(self.system, self.build_user_prompt(
            utterance, history, slots, completed, continuation=continuation))
        return self._checked_resolution(continuation['request'] if continuation else utterance, data,
                                        require_calls=bool(continuation))

    async def repair(self, utterance: str, history: list[dict[str, str]],
                     slots: dict[str, Any], completed: list[dict[str, Any]],
                     rejected: PlanContractError, *,
                     continuation: dict[str, str] | None = None) -> Resolution:
        prompt = self.build_user_prompt(utterance, history, slots, completed, continuation=continuation)
        prompt += ("\n\nPREVIOUS PLAN (rejected before any tool execution)\n"
                   + json.dumps(rejected.rejected_plan, ensure_ascii=False)
                   + "\nPLAN CONTRACT ERROR\n" + rejected.reason
                   + "\nReturn a corrected full JSON plan for the original request. Preserve all requested "
                     "actions and questions, corrections, constraints, and uncertainty. Do not claim tool results.")
        data = await self.llm.complete_json(self.system, prompt)
        return self._checked_resolution(continuation['request'] if continuation else utterance, data,
                                        require_calls=bool(continuation))

    def _checked_resolution(self, utterance: str, data: dict[str, Any], *,
                            require_calls: bool = False) -> Resolution:
        resolution = self._to_resolution(data)
        if require_calls and (not resolution.complete or not resolution.calls):
            raise PlanContractError("The grounded continued request still requires its requested action; "
                                    "an incomplete or empty plan does not complete it.", data)
        if self.manifest.plan_validator is not None:
            reason = self.manifest.plan_validator(utterance, resolution.complete, resolution.calls)
            if reason:
                raise PlanContractError(reason, data)
        return resolution

    def _to_resolution(self, data: dict[str, Any]) -> Resolution:
        # Reject malformed output as a whole: dropping one call can turn a
        # dependent or qualified request into a different, executable action.
        if not isinstance(data, dict):
            raise ValueError("plan must be an object")
        complete = data.get("complete", True)
        raw_calls = data.get("calls", [])
        corrections = data.get("corrections", [])
        reply = data.get("reply", "")
        repaired = data.get("repaired", "")
        if not isinstance(complete, bool):
            raise ValueError("plan complete must be a boolean")
        if not isinstance(raw_calls, list):
            raise ValueError("plan calls must be a list")
        if not isinstance(corrections, list) or any(not isinstance(c, dict) for c in corrections):
            raise ValueError("plan corrections must be a list of objects")
        if not isinstance(reply, str) or not isinstance(repaired, str):
            raise ValueError("plan reply and repaired must be strings")
        calls: list[PlannedCall] = []
        for i, c in enumerate(raw_calls):
            if not isinstance(c, dict):
                raise ValueError("each planned call must be an object")
            tool = c.get("tool", c.get("function", c.get("name")))
            if not isinstance(tool, str) or self.manifest.get(tool) is None:
                raise ValueError(f"unknown planned tool: {tool}")
            args = c.get("args", c.get("arguments", {}))
            if not isinstance(args, dict):
                raise ValueError(f"arguments for {tool} must be an object")
            spec = self.manifest.get(tool)
            if any(_placeholder(args.get(k)) for k in spec.required):
                raise ValueError(f"missing required detail for {tool}")
            args = self.manifest.coerce_args(tool, args, defer_references=True)
            call_id = c.get("id", f"c{i + 1}")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError("planned call IDs must be nonempty strings")
            calls.append(PlannedCall(id=call_id, tool=tool, args=args))
        return Resolution(
            complete=complete,
            repaired=repaired,
            corrections=corrections,
            # slots = the argument values the plan will use (latest wins)
            slots={k: v for c in calls for k, v in c.args.items()
                   if not (isinstance(v, str) and v.startswith("$"))},
            calls=calls,
            reply=reply.strip(),
            raw=data,
        )
