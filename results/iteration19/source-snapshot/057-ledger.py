"""In-memory operation ledger; this is not durable backend idempotency.

Successful writes deduplicate within one operation, not across later intentions.
Uncertain or in-flight writes prevent an identical action from being silently
retried in any operation. Reconciliation requires backend-specific evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def action_key(tool: str, args: dict[str, Any]) -> str:
    """Stable mapping order without changing opaque identifiers or values."""
    return json.dumps([tool, args], sort_keys=True)


def _operation_key(operation_id: str, tool: str, args: dict[str, Any]) -> str:
    return json.dumps([operation_id, action_key(tool, args)])


def _read_key(tool: str, args: dict[str, Any], context: Any) -> str:
    return json.dumps([action_key(tool, args), context], sort_keys=True)


@dataclass
class LedgerEntry:
    key: str
    tool: str
    args: dict[str, Any]
    result: dict[str, Any] | None
    call_id: str
    operation_id: str = ""
    status: str = "done"
    error: str | None = None


@dataclass
class Ledger:
    entries: dict[str, LedgerEntry] = field(default_factory=dict)
    _inflight: dict[str, LedgerEntry] = field(default_factory=dict)
    _call_actions: dict[tuple[str, str], str] = field(default_factory=dict)
    reads: dict[str, dict[str, Any]] = field(default_factory=dict)

    def identity_conflicts(self, operation_id: str, call_id: str, tool: str, args: dict[str, Any]) -> bool:
        prior = self._call_actions.get((operation_id, call_id))
        return prior is not None and prior != action_key(tool, args)

    def cached_read(self, tool: str, args: dict[str, Any], context: Any = None) -> dict[str, Any] | None:
        return self.reads.get(_read_key(tool, args, context))

    def remember_read(self, tool: str, args: dict[str, Any], result: dict[str, Any], context: Any = None) -> None:
        self.reads[_read_key(tool, args, context)] = result

    def check(self, tool: str, args: dict[str, Any], operation_id: str = "") -> LedgerEntry | None:
        key = _operation_key(operation_id, tool, args)
        if key in self.entries:
            return self.entries[key]
        action = action_key(tool, args)
        for entry in (*self._inflight.values(), *self.entries.values()):
            if entry.status in ("pending", "unknown") and action_key(entry.tool, entry.args) == action:
                return entry
        return None

    def begin(self, tool: str, args: dict[str, Any], operation_id: str = "", call_id: str = "") -> str:
        key = _operation_key(operation_id, tool, args)
        self._call_actions[(operation_id, call_id)] = action_key(tool, args)
        self._inflight[key] = LedgerEntry(key, tool, args, None, call_id, operation_id, "pending")
        return key

    def commit(self, key: str, tool: str, args: dict[str, Any], result: dict[str, Any], call_id: str) -> None:
        # A cancellation-resistant backend may acknowledge after shutdown marked
        # the operation unknown. That acknowledgment resolves the uncertainty.
        entry = self._inflight.pop(key, None) or self.entries[key]
        self.entries[key] = LedgerEntry(key, tool, args, result, call_id, entry.operation_id)

    def abort(self, key: str) -> None:
        """Release a write known to have failed or not reached the backend."""
        self._inflight.pop(key, None)
        if key in self.entries and self.entries[key].status == "unknown":
            del self.entries[key]

    def unknown(self, key: str, error: str) -> None:
        entry = self._inflight.pop(key, None) or self.entries[key]
        entry.status, entry.error = "unknown", error
        self.entries[key] = entry

    def forget(self, tools: tuple[str, ...]) -> None:
        """A superseding action (e.g. cancel navigation) makes earlier actions repeatable."""
        self.entries = {k: e for k, e in self.entries.items() if e.tool not in tools or e.status == "unknown"}

    def summary(self) -> list[dict[str, Any]]:
        return [{"tool": e.tool, "args": e.args, "result": e.result, "status": e.status,
                 "operation_id": e.operation_id, "error": e.error} for e in self.entries.values()]
