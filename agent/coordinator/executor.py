"""Slow path: run a plan of tool calls as an asyncio DAG.

- Calls whose args reference earlier results ($c1.x) wait for those calls.
- Independent calls run concurrently.
- cancel_stale() cancels in-flight read-only calls (their result is no longer
  wanted). A state-changing call that has already started is protected from
  cancellation and allowed to finish, so the system never ends up in an unknown state; the
  ledger then prevents it from being repeated.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import events as E
from .events import EventBus
from .ledger import Ledger
from .refs import UnresolvedRef, deps_of, resolve_args
from ..tools.manifest import Manifest

ToolLogger = Callable[[str, dict[str, Any], float, float], None]


@dataclass
class PlannedCall:
    id: str
    tool: str
    args: dict[str, Any]


@dataclass
class CallOutcome:
    id: str
    tool: str
    args: dict[str, Any]
    status: str                      # done | reused | blocked | error | cancelled | skipped
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class Execution:
    calls: list[PlannedCall]
    outcomes: dict[str, CallOutcome] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    protected: set[str] = field(default_factory=set)   # started state-changing calls
    first_tool_start: float | None = None
    last_tool_end: float | None = None


class Executor:
    def __init__(self, manifest: Manifest, ledger: Ledger, bus: EventBus, tool_logger: ToolLogger) -> None:
        self.manifest = manifest
        self.ledger = ledger
        self.bus = bus
        self.tool_logger = tool_logger
        self._current: Execution | None = None

    async def run(self, calls: list[PlannedCall]) -> Execution:
        ex = Execution(calls=calls)
        self._current = ex
        results: dict[str, Any] = {}
        done_events: dict[str, asyncio.Event] = {c.id: asyncio.Event() for c in calls}

        async def run_one(c: PlannedCall) -> None:
            try:
                for dep in deps_of(c.args):
                    if dep in done_events:
                        await done_events[dep].wait()
                try:
                    args = resolve_args(c.args, results)
                except UnresolvedRef as e:
                    ex.outcomes[c.id] = CallOutcome(c.id, c.tool, c.args, "skipped", error=f"unresolved {e}")
                    self.bus.emit(E.TOOL_ERROR, id=c.id, tool=c.tool, error=f"unresolved reference {e}")
                    return
                args = self.manifest.coerce_args(c.tool, args)
                await self._call(ex, c, args, results)
            finally:
                done_events[c.id].set()

        for c in calls:
            ex.tasks[c.id] = asyncio.create_task(run_one(c))
        if ex.tasks:
            await asyncio.gather(*ex.tasks.values(), return_exceptions=True)
        for c in calls:
            ex.outcomes.setdefault(c.id, CallOutcome(c.id, c.tool, c.args, "cancelled"))
        return ex

    async def _call(self, ex: Execution, c: PlannedCall, args: dict[str, Any], results: dict[str, Any]) -> None:
        spec = self.manifest.get(c.tool)
        if spec is None:
            ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "error", error="unknown tool")
            self.bus.emit(E.TOOL_ERROR, id=c.id, tool=c.tool, error="unknown tool")
            return

        key = None
        if not spec.state_changing:
            cached = self.ledger.cached_read(c.tool, args)
            if cached is not None:
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "reused", result=cached)
                results[c.id] = cached
                self.bus.emit(E.TOOL_REUSED, id=c.id, tool=c.tool, args=args,
                              reason="same lookup already answered in this conversation")
                return
        if spec.state_changing:
            prior = self.ledger.check(c.tool, args)
            if prior is not None:
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "blocked", result=prior.result)
                if prior.result is not None:
                    results[c.id] = prior.result
                self.bus.emit(E.TOOL_BLOCKED, id=c.id, tool=c.tool, args=args,
                              reason="identical state-changing action already performed")
                return
            key = self.ledger.begin(c.tool, args)

        if spec.state_changing:
            ex.protected.add(c.id)
        self.bus.emit(E.TOOL_STARTED, id=c.id, tool=c.tool, args=args, state_changing=spec.state_changing)
        t0 = time.time()
        ex.first_tool_start = ex.first_tool_start or t0
        try:
            result = await spec.fn(**args)
        except asyncio.CancelledError:
            if key:
                self.ledger.abort(key)
            ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "cancelled")
            self.bus.emit(E.TOOL_CANCELLED, id=c.id, tool=c.tool, args=args, reason="stale")
            raise
        except Exception as e:  # tool failure is reported, never crashes the session
            if key:
                self.ledger.abort(key)
            ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "error", error=str(e))
            self.bus.emit(E.TOOL_ERROR, id=c.id, tool=c.tool, error=str(e))
            return
        t1 = time.time()
        ex.last_tool_end = t1
        self.tool_logger(c.tool, args, t0, t1)
        if key:
            self.ledger.commit(key, c.tool, args, result, c.id)
            if spec.resets and isinstance(result, dict) and result.get("status") != "error":
                self.ledger.forget(spec.resets)
        elif isinstance(result, dict) and result.get("status") != "error":
            self.ledger.remember_read(c.tool, args, result)
        results[c.id] = result
        ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "done", result=result)
        self.bus.emit(E.TOOL_DONE, id=c.id, tool=c.tool, args=args, result=result,
                      duration_ms=round((t1 - t0) * 1000))

    def cancel_stale(self) -> int:
        """Cancel every unfinished call of the current execution. Returns count."""
        ex = self._current
        if ex is None:
            return 0
        n = 0
        for cid, task in ex.tasks.items():
            if not task.done() and cid not in ex.protected:
                task.cancel()
                n += 1
        return n
