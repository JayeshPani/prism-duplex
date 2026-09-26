"""Typed coordinator events.

Every decision the coordinator makes is emitted as an event. Events go to the
per-room JSONL trace (debugging, replay) and to the web UI over the LiveKit
data channel (topic "agent-events").
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Event type names (kept as constants so the web UI and tests agree on them)
USER_PARTIAL = "user_partial"          # interim transcript
USER_FINAL = "user_final"              # finalized transcript segment
USER_RESUMED = "user_resumed"          # user started speaking again while a turn was pending
TURN_MERGED = "turn_merged"            # new segment merged into a still-pending utterance
PLAN_READY = "plan_ready"              # resolver output (repaired request, slots, calls)
SLOT_UPDATE = "slot_update"            # slot changed (with previous value = correction)
GATE_OPEN = "gate_open"                # commit gate started holding
GATE_CANCELLED = "gate_cancelled"      # held plan discarded (user kept talking / corrected)
GATE_COMMITTED = "gate_committed"      # plan released to the executor
TOOL_STARTED = "tool_started"
TOOL_DONE = "tool_done"
TOOL_CANCELLED = "tool_cancelled"      # in-flight call cancelled as stale
TOOL_BLOCKED = "tool_blocked"          # duplicate state-changing call prevented
TOOL_REUSED = "tool_reused"            # identical lookup answered from this conversation's earlier result
TOOL_UNKNOWN = "tool_unknown"          # effect may have happened; reconcile before retry
TOOL_ERROR = "tool_error"
AGENT_SAY = "agent_say"                # text handed to TTS
ACK = "ack"                            # fast-path filler

EventSink = Callable[[Event], None]


class EventBus:
    """Fan-out of events to any number of sinks. Sinks must not raise."""

    def __init__(self) -> None:
        self._sinks: list[EventSink] = []
        self.history: list[Event] = []

    def subscribe(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def emit(self, type_: str, **data: Any) -> Event:
        ev = Event(type=type_, data=data)
        self.history.append(ev)
        for sink in self._sinks:
            try:
                sink(ev)
            except Exception:  # a broken sink must never break the conversation
                pass
        return ev

    def of_type(self, type_: str) -> list[Event]:
        return [e for e in self.history if e.type == type_]
