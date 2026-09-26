"""Keep a tentative speech pause until local recognition resolves it.

LiveKit's false-interruption timer can expire before a non-streaming recognizer
has supplied any transcript. Suppress downstream resume, rather than re-pausing
after it: an already awakened audio waiter cannot be put back to sleep.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from livekit import rtc
from livekit.agents.voice import io


class RecognitionAwareAudioOutput(io.AudioOutput):
    def __init__(self, sink: io.AudioOutput, *, current_speech: Callable,
                 pending_recognitions: Callable[[], int], user_speaking: Callable[[], bool],
                 emit: Callable, max_hold_seconds: float = 30.0) -> None:
        super().__init__(label="RecognitionAwareAudioOutput",
                         capabilities=io.AudioOutputCapabilities(pause=True),
                         next_in_chain=sink, sample_rate=sink.sample_rate)
        self._current_speech = current_speech
        self._pending_recognitions = pending_recognitions
        self._user_speaking = user_speaking
        self._emit = emit
        self._max_hold_seconds = max_hold_seconds
        self._paused_handle = None
        self._held_handle = None
        self._deadline = None
        self._sdk_paused = False
        self._resume_requested = False
        self._cancelling = False
        self._closed = False

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        await self.next_in_chain.capture_frame(frame)

    def flush(self) -> None:
        super().flush()
        self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        self.next_in_chain.clear_buffer()

    def pause(self) -> None:
        self._sdk_paused = True
        self._resume_requested = False
        self._paused_handle = self._current_speech()
        if self._held_handle is not None and self._held_handle is not self._paused_handle:
            self._forget_hold("speech_changed")
        super().pause()

    def resume(self) -> None:
        if self._closed:
            return
        self._sdk_paused = False
        self._resume_requested = True
        handle = self._current_speech()
        if (self._held_handle is None and handle is not None
                and handle is self._paused_handle and not handle.done()
                and not handle.interrupted and handle.allow_interruptions
                and self.can_pause and self._pending_recognitions()):
            self._held_handle = handle
            handle.add_done_callback(self._speech_done)
            self._deadline = asyncio.get_running_loop().call_later(
                self._max_hold_seconds, self._hold_expired, handle)
            self._emit("recognition_pause", state="held", speech_id=handle.id,
                       pending_recognitions=self._pending_recognitions())
        if self._held_handle is None:
            # Preserve ordinary SDK resume decisions outside an owned hold.
            self._resume_requested = False
            self._paused_handle = None
            super().resume()
            return
        self._maybe_resume()

    def recognition_completed(self, status: str, text: str | None) -> None:
        if self._closed or self._held_handle is None:
            return
        if status != "returned" or (text and text.strip()):
            self._interrupt_held("transcript" if status == "returned" else status)
        else:
            self._maybe_resume()

    def user_state_changed(self) -> None:
        self._maybe_resume()

    def _interrupt_held(self, reason: str) -> None:
        handle = self._held_handle
        if handle is None:
            return
        if handle is not self._current_speech() or handle.done():
            self._forget_hold("speech_changed")
            self._maybe_resume()
            return
        if not handle.allow_interruptions:
            self._forget_hold("interruptions_disabled")
            self._maybe_resume()
            return
        self._cancelling = True
        if self._deadline:
            self._deadline.cancel()
            self._deadline = None
        handle.interrupt()
        self._emit("recognition_pause", state="interruption_requested",
                   speech_id=handle.id, reason=reason)
        # Resume only after this handle's generation has cleared its old audio.
        # A cancellation request alone is not a completed playout cleanup.

    def _hold_expired(self, handle) -> None:
        if not self._closed and self._held_handle is handle:
            self._interrupt_held("recognition_wait_timeout")

    def _speech_done(self, handle) -> None:
        if self._held_handle is handle:
            self._forget_hold("speech_finished")
            self._maybe_resume()

    def _forget_hold(self, reason: str) -> None:
        handle, self._held_handle = self._held_handle, None
        self._cancelling = False
        if self._deadline:
            self._deadline.cancel()
            self._deadline = None
        if handle is not None:
            handle.remove_done_callback(self._speech_done)
            self._emit("recognition_pause", state="released", speech_id=handle.id,
                       reason=reason)

    def _maybe_resume(self) -> None:
        if self._closed:
            return
        handle = self._held_handle
        if handle is not None:
            if handle is not self._current_speech() or handle.done():
                self._forget_hold("speech_changed")
            elif not handle.allow_interruptions:
                self._forget_hold("interruptions_disabled")
            elif self._cancelling or self._pending_recognitions():
                return
            elif not self._sdk_paused and not self._user_speaking():
                self._forget_hold("recognition_empty")
            else:
                return
        if self._resume_requested and not self._sdk_paused and not self._user_speaking():
            self._resume_requested = False
            self._paused_handle = None
            super().resume()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle = self._held_handle
        if (handle is not None and handle is self._current_speech()
                and not handle.done() and handle.allow_interruptions):
            handle.interrupt()
        self._forget_hold("closed")
        self._resume_requested = False

    def on_detached(self) -> None:
        self.close()
        super().on_detached()
