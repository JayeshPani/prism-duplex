"""Validated tool DAGs with operation identities and conservative write outcomes.

Corrections cancel reads and writes that have not dispatched. Already dispatched
writes finish in order; a timeout, cancellation or exception has an unknown effect
unless the backend explicitly reports failure. This session ledger is not durable.
"""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from uuid import uuid4

from . import events as E
from .events import EventBus
from .ledger import Ledger
from .refs import UnresolvedRef, deps_of, resolve_args
from ..tools.manifest import Manifest

ToolLogger = Callable[[str, dict[str, Any], float, float], None]


class ToolAttemptLogger(Protocol):
    def started(self, attempt_id: str, function: str, args: dict[str, Any], t_start: float) -> None: ...
    def finished(self, attempt_id: str, t_end: float, status: str) -> None: ...


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
    status: str  # done | reused | blocked | error | unknown | cancelled | skipped
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class Execution:
    calls: list[PlannedCall]
    outcomes: dict[str, CallOutcome] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    protected: set[str] = field(default_factory=set)
    write_keys: dict[str, str] = field(default_factory=dict)
    first_tool_start: float | None = None
    last_tool_end: float | None = None
    execution_id: str = field(default_factory=lambda: uuid4().hex)
    operation_id: str = ""
    stale: bool = False
    validation_error: str | None = None


class Executor:
    def __init__(self, manifest: Manifest, ledger: Ledger, bus: EventBus,
                 tool_logger: ToolLogger | None = None, *,
                 attempt_logger: ToolAttemptLogger | None = None) -> None:
        self.manifest = manifest
        self.ledger = ledger
        self.bus = bus
        self.tool_logger = tool_logger
        self.attempt_logger = attempt_logger
        self._active: dict[str, Execution] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False

    def _emit(self, ex: Execution, type_: str, **data: Any) -> None:
        self.bus.emit(type_, execution_id=ex.execution_id, operation_id=ex.operation_id, **data)

    def _validate(self, calls: list[PlannedCall]) -> None:
        ids = [c.id for c in calls]
        if any(not isinstance(cid, str) or not cid for cid in ids) or len(set(ids)) != len(ids):
            raise ValueError("call IDs must be unique, nonempty strings")
        if any(not isinstance(c.args, dict) for c in calls):
            raise ValueError("call arguments must be objects")
        dependencies = {c.id: deps_of(c.args) for c in calls}
        for c in calls:
            spec = self.manifest.get(c.tool)
            if spec is None:
                raise ValueError(f"unknown tool: {c.tool}")
            self.manifest.coerce_args(c.tool, c.args, defer_references=True)
            missing = dependencies[c.id] - set(ids)
            if missing:
                raise ValueError(f"unknown dependencies for {c.id}: {sorted(missing)}")
        remaining = dict(dependencies)
        while remaining:
            ready = {cid for cid, deps in remaining.items() if not deps & remaining.keys()}
            if not ready:
                raise ValueError("cyclic call dependencies")
            remaining = {cid: deps for cid, deps in remaining.items() if cid not in ready}

    async def run(self, calls: list[PlannedCall], *, operation_id: str | None = None,
                  is_current: Callable[[], bool] | None = None) -> Execution:
        """Use a stable operation_id only when retrying the same intention."""
        ex = Execution(calls=calls, operation_id=operation_id or uuid4().hex)
        try:
            if self._closed:
                raise ValueError("executor is closed")
            self._validate(calls)
        except ValueError as error:
            ex.validation_error = str(error)
            for c in calls:
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, c.args, "error", error=str(error))
                self._emit(ex, E.TOOL_ERROR, id=c.id, tool=c.tool, error=str(error))
            return ex

        self._active[ex.execution_id] = ex
        results: dict[str, Any] = {}
        done_events = {c.id: asyncio.Event() for c in calls}

        def current() -> bool:
            return not ex.stale and not self._closed and (is_current is None or is_current())

        async def run_one(c: PlannedCall) -> None:
            try:
                for dep in deps_of(c.args):
                    await done_events[dep].wait()
                args = self.manifest.coerce_args(c.tool, resolve_args(c.args, results))
                spec = self.manifest.tools[c.tool]
                if spec.state_changing:
                    async with self._write_lock:
                        await self._call(ex, c, args, results, current)
                else:
                    await self._call(ex, c, args, results, current)
            except UnresolvedRef as error:
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, c.args, "skipped", error=f"unresolved {error}")
                self._emit(ex, E.TOOL_ERROR, id=c.id, tool=c.tool, error=f"unresolved reference {error}")
            except asyncio.CancelledError:
                if c.id not in ex.outcomes:
                    ex.outcomes[c.id] = CallOutcome(c.id, c.tool, c.args, "cancelled")
                    self._emit(ex, E.TOOL_CANCELLED, id=c.id, tool=c.tool, args=c.args, reason="stale before dispatch")
            except Exception as error:
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, c.args, "error", error=str(error))
                self._emit(ex, E.TOOL_ERROR, id=c.id, tool=c.tool, error=str(error))
            finally:
                done_events[c.id].set()

        for c in calls:
            self._emit(ex, "tool_waiting", id=c.id, tool=c.tool, args=c.args)
            ex.tasks[c.id] = asyncio.create_task(run_one(c))
        try:
            if ex.tasks:
                # Cancelling the caller must not accidentally cancel an already
                # dispatched write. aclose owns explicit shutdown cancellation.
                await asyncio.shield(asyncio.gather(*ex.tasks.values(), return_exceptions=True))
        except asyncio.CancelledError:
            ex.stale = True
            self._cancel_execution(ex)
            raise
        finally:
            # Keep cancelled parent executions discoverable until all child
            # tasks finish, so later corrections/shutdown can still reach them.
            def discard_if_finished(_task: asyncio.Task | None = None) -> None:
                if all(t.done() for t in ex.tasks.values()):
                    self._active.pop(ex.execution_id, None)

            for task in ex.tasks.values():
                task.add_done_callback(discard_if_finished)
            discard_if_finished()
        for c in calls:
            ex.outcomes.setdefault(c.id, CallOutcome(c.id, c.tool, c.args, "cancelled"))
        return ex

    async def _call(self, ex: Execution, c: PlannedCall, args: dict[str, Any],
                    results: dict[str, Any], current: Callable[[], bool]) -> None:
        if not current():
            raise asyncio.CancelledError
        spec = self.manifest.tools[c.tool]
        context = deepcopy(spec.cache_context()) if spec.cache_context is not None else None
        if not spec.state_changing and spec.cache_context is not None:
            cached = self.ledger.cached_read(c.tool, args, context)
            if cached is not None:
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "reused", result=cached)
                results[c.id] = cached
                self._emit(ex, E.TOOL_REUSED, id=c.id, tool=c.tool, args=args,
                           reason="same lookup in the same declared cache context")
                return
        if spec.state_changing:
            if self.ledger.identity_conflicts(ex.operation_id, c.id, c.tool, args):
                error = "operation call identity was reused with different write arguments"
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "error", error=error)
                self._emit(ex, E.TOOL_ERROR, id=c.id, tool=c.tool, args=args, error=error)
                return
            prior = self.ledger.check(c.tool, args, ex.operation_id)
            if prior is not None:
                status = "blocked" if prior.status == "done" else "unknown"
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, status, result=prior.result, error=prior.error)
                if status == "blocked":
                    results[c.id] = prior.result
                self._emit(ex, E.TOOL_BLOCKED if status == "blocked" else "tool_unknown",
                           id=c.id, tool=c.tool, args=args, result=prior.result,
                           reason="same operation already completed" if status == "blocked" else "prior effect is unconfirmed; not retried")
                return

        # No await between this final version check and invoking the tool.
        # Synchronous event sinks can receive a correction, so emit first.
        self._emit(ex, E.TOOL_STARTED, id=c.id, tool=c.tool, args=args, state_changing=spec.state_changing)
        if not current():
            raise asyncio.CancelledError
        key = self.ledger.begin(c.tool, args, ex.operation_id, c.id) if spec.state_changing else None
        if spec.state_changing:
            ex.protected.add(c.id)
            ex.write_keys[c.id] = key
        t0 = time.time()
        attempt_id = f"{ex.execution_id}:{c.id}"
        if self.attempt_logger is not None:
            try:
                self.attempt_logger.started(attempt_id, c.tool, args, t0)
            except Exception as error:
                # Do not run an action whose dispatch evidence could not be saved.
                # A partially successful logger may retain an unconfirmed intent.
                if key:
                    self.ledger.abort(key)
                ex.protected.discard(c.id)
                ex.write_keys.pop(c.id, None)
                reason = f"tool dispatch logging failed; action not dispatched: {error}"
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "error", error=reason)
                self._emit(ex, "tool_log_error", id=c.id, tool=c.tool, error=reason)
                self._emit(ex, E.TOOL_ERROR, id=c.id, tool=c.tool, args=args, error=reason)
                return
        ex.first_tool_start = ex.first_tool_start or t0
        try:
            try:
                result = await spec.fn(**args)
            except asyncio.CancelledError:
                status = "unknown" if key else "cancelled"
                reason = "write cancelled after dispatch; effect is unconfirmed" if key else "stale read"
                if key:
                    self.ledger.unknown(key, reason)
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, status, error=reason)
                self._emit(ex, "tool_unknown" if key else E.TOOL_CANCELLED,
                           id=c.id, tool=c.tool, args=args, reason=reason)
                raise
            except Exception as error:
                status = "unknown" if key else "error"
                if key:
                    self.ledger.unknown(key, str(error))
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, status, error=str(error))
                self._emit(ex, "tool_unknown" if key else E.TOOL_ERROR,
                           id=c.id, tool=c.tool, args=args, error=str(error))
                return

            if not isinstance(result, dict) or result.get("status") in ("error", "failed", "unknown"):
                unknown = bool(key) and (not isinstance(result, dict) or result.get("status") == "unknown")
                status = "unknown" if unknown else "error"
                error = str(result.get("error", result.get("message", "tool reported failure"))) if isinstance(result, dict) else "invalid tool response"
                if key:
                    if unknown:
                        self.ledger.unknown(key, error)
                    else:
                        self.ledger.abort(key)
                ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, status,
                                              result=result if isinstance(result, dict) else None, error=error)
                self._emit(ex, "tool_unknown" if unknown else E.TOOL_ERROR,
                           id=c.id, tool=c.tool, args=args, result=result, error=error)
                return
            if key:
                self.ledger.commit(key, c.tool, args, result, c.id)
            elif spec.cache_context is not None and current():
                self.ledger.remember_read(c.tool, args, result, context)
            results[c.id] = result
            ex.outcomes[c.id] = CallOutcome(c.id, c.tool, args, "done", result=result)
            self._emit(ex, E.TOOL_DONE, id=c.id, tool=c.tool, args=args, result=result,
                       stale=not current(), duration_ms=round((time.time() - t0) * 1000))
        finally:
            t1 = time.time()
            ex.last_tool_end = t1
            ex.protected.discard(c.id)
            ex.write_keys.pop(c.id, None)
            if self.attempt_logger is not None:
                try:
                    outcome = ex.outcomes.get(c.id)
                    self.attempt_logger.finished(attempt_id, t1, outcome.status if outcome else "unknown")
                except Exception as error:
                    self._emit(ex, "tool_log_error", id=c.id, tool=c.tool, error=f"tool completion logging failed: {error}")
            if self.tool_logger is not None:
                try:
                    self.tool_logger(c.tool, args, t0, t1)
                except Exception as error:
                    self._emit(ex, "tool_log_error", id=c.id, tool=c.tool, error=f"tool logging failed: {error}")

    def _cancel_execution(self, ex: Execution) -> int:
        ex.stale = True
        n = 0
        for cid, task in ex.tasks.items():
            if not task.done() and cid not in ex.protected and not task.cancelling():
                task.cancel()
                n += 1
        return n

    def cancel_stale(self) -> int:
        """Cancel pending calls and reads across every active execution."""
        return sum(self._cancel_execution(ex) for ex in self._active.values())

    async def aclose(self, timeout: float = 5.0) -> bool:
        """Drain protected writes, then cancel. False means a tool resisted cancellation.

        A coroutine that suppresses cancellation cannot be forcibly stopped by
        asyncio; the backend must reconcile its effect. At most two timeout
        windows are awaited, the second allowing cancellation/logging cleanup.
        """
        self._closed = True
        self.cancel_stale()
        tasks = {t for ex in self._active.values() for t in ex.tasks.values() if not t.done()}
        if not tasks:
            return True
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for ex in self._active.values():
            for cid, key in ex.write_keys.items():
                if ex.tasks[cid] in pending:
                    reason = "shutdown deadline exceeded; write effect is unconfirmed"
                    self.ledger.unknown(key, reason)
                    call = next(c for c in ex.calls if c.id == cid)
                    ex.outcomes[cid] = CallOutcome(cid, call.tool, call.args, "unknown", error=reason)
                    self._emit(ex, "tool_unknown", id=cid, tool=call.tool, reason=reason)
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=timeout)
        return not pending
