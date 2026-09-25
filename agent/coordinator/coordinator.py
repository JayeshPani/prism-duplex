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
    `-- commit: cancel stale in-flight reads, speak the plan sentence, run the
        calls (ledger blocks duplicates), then speak a grounded result summary.

Nothing is executed before the commit, so a superseded value never reaches a
tool, and every executed call is a call the user finally asked for.
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
        self._floor_free = asyncio.Event()       # cleared while the user is talking
        self._floor_free.set()
        self._reopen_task: asyncio.Task | None = None
        self._commit_tasks: set[asyncio.Task] = set()
        self._turn_idx = 0
        self._spoke_this_turn = False
        self.user_done_at: float | None = None
        self.first_speech_at: float | None = None

    # ── inputs from the audio pipeline ───────────────────────────────
    def on_user_speaking(self) -> None:
        """VAD: the user started talking. Pause any held plan until we know
        whether new words (a correction?) follow or it was just noise."""
        if self._reopen_task:
            self._reopen_task.cancel()
        if self._gate_task and not self._gate_task.done():
            self._floor_free.clear()
            self.bus.emit(E.USER_RESUMED, pending=" ".join(self._pending))

    def on_user_listening(self) -> None:
        """VAD: the user stopped. If no new transcript arrives soon, release the paused plan."""
        if self._floor_free.is_set():
            return
        if self._reopen_task:
            self._reopen_task.cancel()

        async def reopen() -> None:
            await asyncio.sleep(self.cfg.resume_grace_ms / 1000)
            self._floor_free.set()

        self._reopen_task = asyncio.create_task(reopen())

    def on_user_partial(self, text: str) -> None:
        self.bus.emit(E.USER_PARTIAL, text=text)

    def on_user_turn(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._reopen_task:
            self._reopen_task.cancel()
        self._floor_free.set()
        if self._gate_task and not self._gate_task.done():
            self._gate_task.cancel()
            self.bus.emit(E.GATE_CANCELLED, reason="user kept talking; re-planning with the new words")
        if self._pending:
            self.bus.emit(E.TURN_MERGED, previous=" ".join(self._pending), added=text)
        self._pending.append(text)
        self.bus.emit(E.USER_FINAL, text=text, merged=" ".join(self._pending))
        self.user_done_at = time.time()
        self._spoke_this_turn = False
        self._gate_task = asyncio.create_task(self._resolve_and_gate(" ".join(self._pending)))

    # ── fast path + gate ─────────────────────────────────────────────
    async def _resolve_and_gate(self, utterance: str) -> None:
        t_eou = time.time()
        ack = asyncio.create_task(self._ack_later())
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

        self.bus.emit(E.PLAN_READY, utterance=utterance, repaired=res.repaired, complete=res.complete,
                      corrections=res.corrections, slots=res.slots,
                      calls=[c.__dict__ for c in res.calls], reply=res.reply)

        state_changing = any(self.executor.manifest.tools[c.tool].state_changing for c in res.calls)
        hold = self.cfg.hold_incomplete_ms if not res.complete else (
            self.cfg.hold_state_ms if state_changing else self.cfg.hold_read_ms)
        remaining = hold / 1000 - (time.time() - t_eou)
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

        # ── commit: from here on the turn is not cancelled by new speech ──
        self._pending.clear()
        self._last_commit_ts = time.time()
        task = asyncio.create_task(self._commit(utterance, res))
        self._commit_tasks.add(task)
        task.add_done_callback(self._commit_tasks.discard)

    async def _ack_later(self) -> None:
        await asyncio.sleep(self.cfg.ack_after_ms / 1000)
        if not self._spoke_this_turn and self._floor_free.is_set():  # never talk over the user
            phrase = self.cfg.ack_phrases[self._turn_idx % len(self.cfg.ack_phrases)]
            self.bus.emit(E.ACK, text=phrase)
            await self._say(phrase)

    # ── slow path ────────────────────────────────────────────────────
    async def _commit(self, utterance: str, res: Resolution) -> None:
        self._turn_idx += 1
        self.bus.emit(E.GATE_COMMITTED, calls=[c.tool for c in res.calls])
        self.history.append({"role": "user", "text": utterance})
        self._update_slots(res)

        stale = self.executor.cancel_stale()
        if stale:
            self.bus.emit(E.GATE_CANCELLED, reason=f"cancelled {stale} stale in-flight call(s)")

        if res.reply:
            await self._say(res.reply, wait=False)
            self.history.append({"role": "assistant", "text": res.reply})

        if not res.calls:
            return
        ex = await self.executor.run(res.calls)
        if self.cfg.speak_results and self.responder is not None:
            summary = await self.responder.summarize(res.repaired or utterance, ex)
            if summary:
                await self._say(summary, wait=False)
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

    async def _say(self, text: str, wait: bool = True) -> None:
        self._spoke_this_turn = True
        if self.first_speech_at is None:
            self.first_speech_at = time.time()
        self.bus.emit(E.AGENT_SAY, text=text)
        coro = self.speak_fn(text)
        if wait:
            await coro
        else:
            asyncio.ensure_future(coro)

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for the in-flight gate/commit work (used by tests and shutdown)."""
        end = time.time() + timeout
        while time.time() < end:
            tasks = [t for t in ([self._gate_task] if self._gate_task else []) + list(self._commit_tasks) if not t.done()]
            if not tasks:
                return
            await asyncio.wait(tasks, timeout=max(0.01, end - time.time()))

    def snapshot(self) -> dict[str, Any]:
        """State snapshot for the UI / trace: intent slots + committed actions."""
        return {"slots": self.slots, "committed": self.ledger.summary(),
                "pending": " ".join(self._pending)}

