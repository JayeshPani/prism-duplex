"""Coordinator: fast path + commit gate + slow path, per conversation.

Timeline of one user turn:

  user stops talking (end-of-utterance)
    |-- fast path: if nothing has been said after `ack_after_ms`, speak a short,
    |              truthful filler ("One moment.") -> no dead air
    |-- resolver (LLM) turns the merged utterance into a plan
    |-- commit gate holds the plan for `hold_*_ms` (longer for state-changing
    |   calls and for utterances that look unfinished)
    |      user starts talking again -> commit paused; if new words arrive the
    |      plan is discarded and they are merged with the old ones and
    |      re-resolved (self-correction safe); if it was only noise/a breath,
    |      the plan commits after `resume_grace_ms` of quiet
    `-- commit: run the calls (ledger blocks duplicates), then speak a grounded
        result summary. Planner narration stays in the trace, not the audio.

Finalized corrections invalidate older intents before further dispatch. Started
writes may still finish; cancellation cannot undo an external effect.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import events as E
from .events import EventBus
from .executor import Execution, Executor
from .ledger import Ledger
from .resolver import IntentResolver, Resolution
from .responder import Responder

Speak = Callable[[str], Awaitable[None]]
Retranscribe = Callable[[float], Awaitable["str | None"]]


@dataclass
class CoordinatorConfig:
    hold_read_ms: int = 350          # hold before read-only calls
    hold_state_ms: int = 700         # hold before any state-changing call
    hold_incomplete_ms: int = 1800   # utterance looks cut off
    ack_after_ms: int = 700          # speak a filler if nothing said by then
    resume_grace_ms: int = 1200      # quiet time after a VAD blip before a paused plan commits
    ack_phrases: tuple[str, ...] = ("One moment.", "Okay, one sec.", "Sure, let me check.")
    speak_results: bool = True       # speak a grounded summary after tools finish


class Coordinator:
    def __init__(self, resolver: IntentResolver, executor: Executor, ledger: Ledger,
                 bus: EventBus, speak: Speak, responder: Responder | None = None,
                 config: CoordinatorConfig | None = None, retranscribe: Retranscribe | None = None) -> None:
        self.resolver = resolver
        self.executor = executor
        self.ledger = ledger
        self.bus = bus
        self.speak_fn = speak
        self.responder = responder
        self.cfg = config or CoordinatorConfig()
        self.retranscribe = retranscribe          # whole-span ASR of uncommitted speech
        self._last_commit_ts = 0.0

        self.history: list[dict[str, str]] = []
        self.slots: dict[str, Any] = {}
        self._pending: list[str] = []            # uncommitted user segments
        self._gate_task: asyncio.Task | None = None
        self._gate_tasks: set[asyncio.Task] = set()
        self._floor_free = asyncio.Event()       # cleared while the user is talking
        self._floor_free.set()
        self._reopen_task: asyncio.Task | None = None
        self._commit_tasks: set[asyncio.Task] = set()
        self._speech_tasks: set[asyncio.Task] = set()
        self._intent_version = 0
        self._closed = False
        self._turn_idx = 0
        self._spoke_this_turn = False
        self.user_done_at: float | None = None
        self.first_speech_at: float | None = None

    # ── inputs from the audio pipeline ───────────────────────────────
    def on_user_speaking(self) -> None:
        """VAD: the user started talking. Pause any held plan until we know
        whether new words (a correction?) follow or it was just noise."""
        if self._closed:
            return
        self._floor_free.clear()
        # LiveKit pauses/resumes playback for tentative VAD interruptions. Only a
        # finalized new intent irreversibly cancels our active speech handles.
        if self._reopen_task:
            self._reopen_task.cancel()
        if self._gate_task and not self._gate_task.done():
            self._floor_free.clear()
            self.bus.emit(E.USER_RESUMED, pending=" ".join(self._pending))

    def on_user_listening(self) -> None:
        """VAD: the user stopped. If no new transcript arrives soon, release the paused plan."""
        if self._closed or self._floor_free.is_set():
            return
        if self._reopen_task:
            self._reopen_task.cancel()

        async def reopen() -> None:
            await asyncio.sleep(self.cfg.resume_grace_ms / 1000)
            self._floor_free.set()

        self._reopen_task = asyncio.create_task(reopen())

    def on_user_partial(self, text: str) -> None:
        if self._closed:
            return
        self.bus.emit(E.USER_PARTIAL, text=text)

    def on_user_turn(self, text: str) -> None:
        text = text.strip()
        if not text or self._closed:
            return
        self._intent_version += 1
        self.executor.cancel_stale()
        self._cancel_speech()
        if self._reopen_task:
            self._reopen_task.cancel()
        self._floor_free.set()
        if self._gate_task and not self._gate_task.done():
            self._gate_task.cancel()
            self.bus.emit(E.GATE_CANCELLED, reason="user kept talking; re-planning with the new words")
        if self._pending:
            self.bus.emit(E.TURN_MERGED, previous=" ".join(self._pending), added=text)
        self._pending.append(text)
        self.bus.emit(E.USER_FINAL, text=text, merged=" ".join(self._pending), intent_version=self._intent_version)
        self.user_done_at = time.time()
        self._spoke_this_turn = False
        self._gate_task = asyncio.create_task(self._resolve_and_gate(" ".join(self._pending), self._intent_version))
        self._gate_tasks.add(self._gate_task)
        self._gate_task.add_done_callback(self._gate_tasks.discard)
        self._gate_task.add_done_callback(
            lambda task, version=self._intent_version: self._report_task_error(task, "gate", version))

    def _report_task_error(self, task: asyncio.Task, phase: str, version: int) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self.bus.emit(E.TOOL_ERROR, id=phase, tool="coordinator", phase=phase,
                          intent_version=version, error=str(error))

    # ── fast path + gate ─────────────────────────────────────────────
    async def _resolve_and_gate(self, utterance: str, version: int) -> None:
        t_eou = time.monotonic()
        ack = asyncio.create_task(self._ack_later(version))
        try:
            if self.retranscribe is not None:
                # a sentence split by pauses reads better when transcribed as one piece
                whole = await self.retranscribe(self._last_commit_ts)
                # trust the one-piece transcript only if it did not lose words
                if whole and len(whole.split()) >= 0.85 * len(utterance.split()) and whole.strip() != utterance:
                    self.bus.emit(E.TURN_MERGED, previous=utterance, added="", retranscribed=whole)
                    utterance = whole.strip()
            res = await self.resolver.resolve(utterance, self.history, self.slots, self.ledger.summary())
        except asyncio.CancelledError:
            ack.cancel()
            raise
        except Exception as e:
            ack.cancel()
            self.bus.emit(E.TOOL_ERROR, id="resolver", tool="resolver", error=str(e))
            res = Resolution(True, utterance, [], {}, [], "Sorry, could you say that again?")

        if version != self._intent_version or self._closed:
            ack.cancel()
            return
        self.bus.emit(E.PLAN_READY, intent_version=version, utterance=utterance, repaired=res.repaired, complete=res.complete,
                      corrections=res.corrections, slots=res.slots,
                      calls=[c.__dict__ for c in res.calls], reply=res.reply)

        state_changing = any(self.executor.manifest.tools[c.tool].state_changing for c in res.calls)
        hold = self.cfg.hold_incomplete_ms if not res.complete else (
            self.cfg.hold_state_ms if state_changing else self.cfg.hold_read_ms)
        remaining = hold / 1000 - (time.monotonic() - t_eou)
        self.bus.emit(E.GATE_OPEN, hold_ms=hold, remaining_ms=max(0, round(remaining * 1000)),
                      state_changing=state_changing, complete=res.complete)
        try:
            if remaining > 0:
                await asyncio.sleep(remaining)
            await self._floor_free.wait()      # paused while the user is talking
        except asyncio.CancelledError:
            ack.cancel()
            raise
        ack.cancel()

        if version != self._intent_version or self._closed:
            return
        if not res.complete:
            # Keep the unfinished utterance available for the next continuation.
            await self._say("Could you finish your request?", version=version, kind="clarification")
            return

        # Commit still checks the intent again at dispatch and response boundaries.
        task = asyncio.create_task(self._commit(utterance, res, version))
        self._commit_tasks.add(task)
        task.add_done_callback(self._commit_tasks.discard)
        task.add_done_callback(lambda task: self._report_task_error(task, "commit", version))

    async def _ack_later(self, version: int) -> None:
        await asyncio.sleep(self.cfg.ack_after_ms / 1000)
        if version == self._intent_version and not self._spoke_this_turn and self._floor_free.is_set():  # never talk over the user
            phrase = self.cfg.ack_phrases[self._turn_idx % len(self.cfg.ack_phrases)]
            self.bus.emit(E.ACK, text=phrase)
            await self._say(phrase, version=version, kind="ack")

    # ── slow path ────────────────────────────────────────────────────
    async def _commit(self, utterance: str, res: Resolution, version: int) -> None:
        if version != self._intent_version or self._closed:
            return
        self._pending.clear()
        self._last_commit_ts = time.time()
        self._turn_idx += 1
        self.bus.emit(E.GATE_COMMITTED, intent_version=version, calls=[c.tool for c in res.calls])
        self.history.append({"role": "user", "text": utterance})
        self._update_slots(res)

        if not res.calls:
            if res.reply:
                await self._say(res.reply, wait=False, version=version, kind="response")
                self.history.append({"role": "assistant", "text": res.reply})
            return
        ex = await self.executor.run(res.calls, operation_id=f"turn-{version}",
                                     is_current=lambda: version == self._intent_version and not self._closed)
        if version == self._intent_version and not self._closed and self.cfg.speak_results and self.responder is not None:
            try:
                summary = await self.responder.summarize(res.repaired or utterance, ex)
            except Exception as error:
                self.bus.emit(E.TOOL_ERROR, id=f"summary-{version}", tool="responder", phase="summary",
                              intent_version=version, error=str(error))
                summary = "I couldn't prepare a spoken summary of the result."
            if summary and version == self._intent_version and not self._closed:
                await self._say(summary, wait=False, version=version, kind="result")
                self.history.append({"role": "assistant", "text": summary})

    def _update_slots(self, res: Resolution) -> None:
        for k, v in res.slots.items():
            old = self.slots.get(k)
            if old != v:
                self.slots[k] = v
                self.bus.emit(E.SLOT_UPDATE, slot=k, value=v, previous=old)
        for c in res.corrections:
            self.bus.emit(E.SLOT_UPDATE, slot=c.get("slot"), value=c.get("to"),
                          previous=c.get("from"), correction=True)

    def _cancel_speech(self) -> None:
        for task in self._speech_tasks:
            task.cancel()

    async def _say(self, text: str, wait: bool = True, *, version: int | None = None,
                   kind: str = "response") -> None:
        version = self._intent_version if version is None else version

        async def deliver() -> None:
            await self._floor_free.wait()
            if version != self._intent_version or self._closed:
                return
            self._spoke_this_turn = True
            if self.first_speech_at is None:
                self.first_speech_at = time.time()
            self.bus.emit(E.AGENT_SAY, text=text, intent_version=version, kind=kind)
            # An event sink can synchronously finalize a correction or close the session.
            if version != self._intent_version or self._closed:
                return
            try:
                await self.speak_fn(text)
            except Exception as error:
                self.bus.emit(E.TOOL_ERROR, id="speech", tool="speech", kind=kind,
                              intent_version=version, error=str(error))

        task = asyncio.create_task(deliver())
        self._speech_tasks.add(task)
        task.add_done_callback(self._speech_tasks.discard)
        if wait:
            await task

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for owned work; a timeout is observable, never a successful drain."""
        end = time.monotonic() + timeout
        while True:
            tasks = [t for t in list(self._gate_tasks)
                     + list(self._commit_tasks) + list(self._speech_tasks) if not t.done()]
            if not tasks:
                return
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("coordinator still has pending work")
            await asyncio.wait(tasks, timeout=remaining)

    async def aclose(self, timeout: float = 5.0) -> None:
        """Close dispatch and bound both backend and coordinator cancellation waits."""
        self._closed = True
        self._intent_version += 1
        for task in (*self._gate_tasks, self._reopen_task):
            if task:
                task.cancel()
        self._cancel_speech()
        settled = await self.executor.aclose(timeout=timeout)
        if not settled:
            self.bus.emit(E.TOOL_UNKNOWN, id="shutdown", tool="session",
                          error="backend tasks resisted cancellation; effects require reconciliation")
        tasks = [t for t in (*self._gate_tasks, self._reopen_task) if t]
        tasks += list(self._commit_tasks) + list(self._speech_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                self.bus.emit(E.TOOL_ERROR, id="shutdown", tool="coordinator",
                              error="coordinator tasks resisted cancellation after shutdown deadline",
                              pending_tasks=len(pending))

    def snapshot(self) -> dict[str, Any]:
        """State snapshot for the UI / trace: intent slots + committed actions."""
        return {"intent_version": self._intent_version, "slots": self.slots, "committed": self.ledger.summary(),
                "pending": " ".join(self._pending)}
