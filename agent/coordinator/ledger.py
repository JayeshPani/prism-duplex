"""Idempotency ledger for state-changing tools.

A state-changing call is keyed by (tool, canonical args). If the same key was
already committed in this session, the call is blocked - the agent reports the
earlier result instead of doing it twice. Read-only calls are not tracked here.
The ledger is session-scoped: a new conversation gets a new ledger.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


def _canon(v: Any) -> Any:
    if isinstance(v, str):
        return re.sub(r"[\s_\-]+", " ", v.strip().lower())
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: _canon(x) for k, x in sorted(v.items())}
    if isinstance(v, list):
        return [_canon(x) for x in v]
    return v


def action_key(tool: str, args: dict[str, Any]) -> str:
    return tool + ":" + json.dumps(_canon({k: v for k, v in args.items() if v is not None}), sort_keys=True)


@dataclass
class LedgerEntry:
    key: str
    tool: str
    args: dict[str, Any]
    result: dict[str, Any] | None
    call_id: str


@dataclass
class Ledger:
    entries: dict[str, LedgerEntry] = field(default_factory=dict)
    _inflight: set[str] = field(default_factory=set)
    reads: dict[str, dict[str, Any]] = field(default_factory=dict)   # session-scoped read results

    def cached_read(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """An identical lookup already answered in this conversation (never across conversations)."""
        return self.reads.get(action_key(tool, args))

    def remember_read(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        self.reads[action_key(tool, args)] = result

    def check(self, tool: str, args: dict[str, Any]) -> LedgerEntry | None:
        """Return the existing entry if this exact action was already done/started."""
        key = action_key(tool, args)
        if key in self.entries:
            return self.entries[key]
        if key in self._inflight:
            return LedgerEntry(key, tool, args, None, "inflight")
        return None

    def begin(self, tool: str, args: dict[str, Any]) -> str:
        key = action_key(tool, args)
        self._inflight.add(key)
        return key

    def commit(self, key: str, tool: str, args: dict[str, Any], result: dict[str, Any], call_id: str) -> None:
        self._inflight.discard(key)
        self.entries[key] = LedgerEntry(key, tool, args, result, call_id)

    def abort(self, key: str) -> None:
        self._inflight.discard(key)

    def forget(self, tools: tuple[str, ...]) -> None:
        """A superseding action (e.g. cancel navigation) makes earlier actions repeatable."""
        self.entries = {k: e for k, e in self.entries.items() if e.tool not in tools}

    def summary(self) -> list[dict[str, Any]]:
        return [{"tool": e.tool, "args": e.args, "result": e.result} for e in self.entries.values()]
