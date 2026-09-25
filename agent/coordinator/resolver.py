"""IntentResolver: disfluent speech -> repaired request + slots + tool plan.

One LLM call per (merged) user utterance. The prompt encodes *general* rules
for disfluency and self-repair; its few-shot examples are our own and use
entities that do not come from any benchmark item.
"""

from __future__ import annotations

import json
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
repeat calls already listed under COMPLETED ACTIONS unless the user explicitly asks again or \
changes a detail. Remarks that only acknowledge or restate an earlier request ("that would be \
great", "yes please", "sounds good") need no new calls.
10. Multi-step requests become a chain. A later call can use a field of an earlier result with \
a reference string "$<call id>.<path>", e.g. "$c1.flights[0].flight_id". Use the result shapes \
shown after "->" in the tool list.
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


class IntentResolver:
    def __init__(self, llm: LLMClient, manifest: Manifest) -> None:
        self.llm = llm
        self.manifest = manifest
        self.system = SYSTEM_TEMPLATE.format(
            tools=manifest.prompt_block(),
            domain_notes=("\nDOMAIN NOTES\n" + manifest.domain_notes) if manifest.domain_notes else "",
        )

    def build_user_prompt(self, utterance: str, history: list[dict[str, str]],
                          slots: dict[str, Any], completed: list[dict[str, Any]]) -> str:
        parts = []
        if history:
            parts.append("CONVERSATION SO FAR\n" + "\n".join(f"{h['role']}: {h['text']}" for h in history[-8:]))
        if slots:
            parts.append("CURRENT SLOTS\n" + json.dumps(slots, ensure_ascii=False))
        if completed:
            parts.append("COMPLETED ACTIONS (already executed this session)\n"
                         + "\n".join(json.dumps(c, ensure_ascii=False) for c in completed[-10:]))
        parts.append(f'LATEST USER UTTERANCE\n"{utterance}"')
        hints = planner_hints(utterance)
        if hints:
            parts.append(hints)
        parts.append("Return the JSON object.")
        return "\n\n".join(parts)

    async def resolve(self, utterance: str, history: list[dict[str, str]],
                      slots: dict[str, Any], completed: list[dict[str, Any]]) -> Resolution:
        data = await self.llm.complete_json(self.system, self.build_user_prompt(utterance, history, slots, completed))
        return self._to_resolution(data)

    def _to_resolution(self, data: dict[str, Any]) -> Resolution:
        calls: list[PlannedCall] = []
        for i, c in enumerate(data.get("calls") or []):
            if not isinstance(c, dict):
                continue
            tool = c.get("tool") or c.get("function") or c.get("name")
            if not tool or self.manifest.get(tool) is None:
                continue  # never execute a tool that is not in the manifest
            args = c.get("args") or c.get("arguments") or {}
            if not isinstance(args, dict):
                continue
            spec = self.manifest.get(tool)
            if any(_placeholder(args.get(k)) for k in spec.required):
                continue  # a required detail was not actually given: never call with a made-up value
            calls.append(PlannedCall(id=str(c.get("id") or f"c{i + 1}"), tool=tool, args=args))
        return Resolution(
            complete=bool(data.get("complete", True)),
            repaired=str(data.get("repaired") or data.get("reply") or ""),
            corrections=[x for x in (data.get("corrections") or []) if isinstance(x, dict)],
            # slots = the argument values the plan will use (latest wins)
            slots={k: v for c in calls for k, v in c.args.items()
                   if not (isinstance(v, str) and v.startswith("$"))},
            calls=calls,
            reply=str(data.get("reply") or "").strip(),
            raw=data,
        )
