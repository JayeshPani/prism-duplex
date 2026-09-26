"""Component tests with SDK output/handles; no RTC connection or model inference."""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

io = pytest.importorskip("livekit.agents.voice.io")
SpeechHandle = pytest.importorskip("livekit.agents.voice.speech_handle").SpeechHandle

from agent.pipeline.recognition_pause import RecognitionAwareAudioOutput


class WaitingSink(io.AudioOutput):
    """An awakened Event waiter deliberately does not recheck its pause flag."""

    def __init__(self, *, can_pause=True):
        super().__init__(label="test sink", capabilities=io.AudioOutputCapabilities(pause=can_pause),
                         next_in_chain=None)
        self.enabled = asyncio.Event()
        self.enabled.set()
        self.waiting = asyncio.Event()
        self.resumes = self.pauses = self.clears = self.flushes = self.forwarded = 0
        self.frames = []

    async def forward_one(self):
        self.waiting.set()
        await self.enabled.wait()
        self.forwarded += 1

    def pause(self):
        self.pauses += 1
        self.enabled.clear()

    def resume(self):
        self.resumes += 1
        self.enabled.set()

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        self.frames.append(frame)

    def flush(self):
        super().flush()
        self.flushes += 1

    def clear_buffer(self):
        self.clears += 1
        self.on_playback_finished(playback_position=0, interrupted=True)


async def settle_callbacks():
    # Event-loop checkpoints, not elapsed-time assertions.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest_asyncio.fixture
async def make_output():
    created = []

    def make(*, pending=1, speaking=False, can_pause=True, max_hold_seconds=30):
        handle = SpeechHandle.create()
        state = SimpleNamespace(current=handle, pending=pending, speaking=speaking)
        sink = WaitingSink(can_pause=can_pause)
        emitted = []
        output = RecognitionAwareAudioOutput(
            sink, current_speech=lambda: state.current,
            pending_recognitions=lambda: state.pending, user_speaking=lambda: state.speaking,
            emit=lambda name, **data: emitted.append((name, data)), max_hold_seconds=max_hold_seconds)
        case = SimpleNamespace(output=output, state=state, sink=sink, handle=handle,
                               handles=[handle], emitted=emitted)
        created.append(case)
        return case

    yield make
    for case in created:
        case.output.close()
        for handle in case.handles:
            # Explicit fixture-only completion; this does not emulate generation cleanup.
            handle._mark_done()
    await settle_callbacks()


def held(case):
    case.output.pause()
    case.output.resume()
    assert not case.sink.enabled.is_set()
    assert case.sink.resumes == 0


@pytest.mark.asyncio
async def test_pending_resume_never_wakes_already_waiting_sink(make_output):
    case = make_output()
    case.output.pause()
    waiter = asyncio.create_task(case.sink.forward_one())
    try:
        await case.sink.waiting.wait()
        case.output.resume()
        case.output.resume()  # Another SDK resume path must also be intercepted.
        await settle_callbacks()
        assert not waiter.done() and case.sink.forwarded == 0
        assert case.sink.resumes == 0
        case.state.pending = 0
        case.output.recognition_completed("returned", "")
        await asyncio.wait_for(waiter, 1)
        assert case.sink.forwarded == 1 and case.sink.resumes == 1
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_empty_result_waits_for_all_recognitions_and_releases_once(make_output):
    case = make_output(pending=2)
    held(case)
    case.state.pending = 1
    case.output.recognition_completed("returned", "")
    case.output.resume()
    assert case.sink.resumes == 0 and not case.handle.interrupted
    case.state.pending = 0
    case.output.recognition_completed("returned", "   ")
    case.output.recognition_completed("returned", "")
    assert case.sink.resumes == 1 and case.sink.enabled.is_set()
    assert not case.handle.interrupted


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text", [("returned", "Change the destination."),
                                         ("failed", ""), ("cancelled", "")])
async def test_terminal_nonempty_or_failure_waits_for_captured_handle_cleanup(make_output, status, text):
    case = make_output()
    held(case)
    case.state.pending = 0
    case.output.recognition_completed(status, text)
    assert case.handle.interrupted and not case.handle.done()
    case.output.resume()
    case.output.recognition_completed("returned", "")
    assert case.sink.resumes == 0 and not case.sink.enabled.is_set()
    case.handle._mark_done()
    await settle_callbacks()
    assert case.sink.resumes == 1 and case.sink.enabled.is_set()


@pytest.mark.asyncio
async def test_new_sdk_pause_invalidates_deferred_empty_release(make_output):
    case = make_output()
    held(case)
    case.output.pause()
    case.state.pending = 0
    case.output.recognition_completed("returned", "")
    case.output.user_state_changed()
    assert case.sink.resumes == 0
    case.output.resume()
    assert case.sink.resumes == 1 and not case.handle.interrupted


@pytest.mark.asyncio
async def test_new_user_speech_blocks_release_until_listening(make_output):
    case = make_output()
    held(case)
    case.state.speaking = True
    case.output.user_state_changed()
    case.state.pending = 0
    case.output.recognition_completed("returned", "")
    assert case.sink.resumes == 0 and not case.handle.interrupted
    case.state.speaking = False
    case.output.user_state_changed()
    assert case.sink.resumes == 1 and not case.handle.interrupted


@pytest.mark.asyncio
async def test_old_done_callback_does_not_release_new_handle_hold(make_output):
    case = make_output()
    held(case)
    case.state.pending = 0
    case.output.recognition_completed("returned", "A new command.")
    new = SpeechHandle.create()
    case.handles.append(new)
    case.state.current, case.state.pending = new, 1
    case.output.pause()
    case.output.resume()
    case.handle._mark_done()
    await settle_callbacks()
    assert not new.interrupted and case.sink.resumes == 0
    case.state.pending = 0
    case.output.recognition_completed("returned", "")
    assert case.sink.resumes == 1 and not new.interrupted


@pytest.mark.asyncio
async def test_replacement_handle_is_not_cancelled_by_owned_result(make_output):
    case = make_output()
    held(case)
    new = SpeechHandle.create()
    case.handles.append(new)
    case.state.current = new
    case.state.pending = 0
    case.output.recognition_completed("returned", "Different destination.")
    assert not new.interrupted
    case.handle._mark_done()
    await settle_callbacks()
    assert not new.interrupted


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["close", "on_detached"])
async def test_closed_or_detached_output_never_resumes_from_late_callbacks(make_output, finish):
    case = make_output()
    held(case)
    getattr(case.output, finish)()
    resumes = case.sink.resumes
    case.state.pending = 0
    case.output.recognition_completed("returned", "")
    case.output.user_state_changed()
    case.handle._mark_done()
    await settle_callbacks()
    assert case.sink.resumes == resumes


@pytest.mark.asyncio
async def test_deadline_interrupts_captured_handle_without_early_resume(make_output, monkeypatch):
    loop = asyncio.get_running_loop()
    original = loop.call_later
    deadlines = []

    def call_later(delay, callback, *args, **kwargs):
        if delay != 12.5:
            return original(delay, callback, *args, **kwargs)
        timer = SimpleNamespace(cancelled=False)
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        deadlines.append((timer, callback, args))
        return timer

    monkeypatch.setattr(loop, "call_later", call_later)
    case = make_output(max_hold_seconds=12.5)
    held(case)
    assert len(deadlines) == 1
    _, callback, args = deadlines[0]
    callback(*args)
    assert case.handle.interrupted and case.sink.resumes == 0
    case.handle._mark_done()
    await settle_callbacks()
    assert case.sink.resumes == 1
    # A stale already-dequeued timer callback must be harmless.
    callback(*args)
    assert case.sink.resumes == 1


@pytest.mark.asyncio
async def test_loss_of_interruptibility_does_not_force_or_leave_owned_hold(make_output):
    case = make_output()
    held(case)
    case.handle.allow_interruptions = False
    case.state.pending = 0
    case.output.recognition_completed("returned", "New command.")
    assert not case.handle.interrupted
    assert case.sink.enabled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["no_pending", "no_handle", "done_handle", "protected_handle", "unsupported_sink"])
async def test_uncovered_cases_preserve_ordinary_resume(make_output, condition):
    case = make_output(pending=0 if condition == "no_pending" else 1,
                       can_pause=condition != "unsupported_sink")
    if condition == "no_handle":
        case.state.current = None
    elif condition == "done_handle":
        case.handle._mark_done()
    elif condition == "protected_handle":
        case.handle.allow_interruptions = False
    case.output.pause()
    case.output.resume()
    assert case.sink.enabled.is_set() and case.sink.resumes == 1
    assert not case.handle.interrupted


@pytest.mark.asyncio
async def test_completion_without_owned_hold_does_not_cancel_speech(make_output):
    case = make_output(pending=0)
    case.output.recognition_completed("returned", "Recognized normally.")
    assert not case.handle.interrupted and case.sink.resumes == 0


@pytest.mark.asyncio
async def test_unowned_sdk_resume_is_unchanged_while_user_speaking(make_output):
    case = make_output(pending=0, speaking=True)
    case.output.pause()
    case.output.resume()
    assert case.sink.resumes == 1 and case.sink.enabled.is_set()
    assert not case.handle.interrupted


@pytest.mark.asyncio
async def test_recognition_becomes_pending_after_sdk_pause(make_output):
    case = make_output(pending=0)
    case.output.pause()
    case.state.pending = 1
    case.output.resume()
    assert not case.sink.enabled.is_set() and case.sink.resumes == 0
    case.state.pending = 0
    case.output.recognition_completed("returned", "")
    assert case.sink.enabled.is_set() and case.sink.resumes == 1


@pytest.mark.asyncio
async def test_room_outputs_do_not_share_recognition_or_release_state(make_output):
    first, second = make_output(), make_output()
    held(first)
    held(second)
    second.state.pending = 0
    second.output.recognition_completed("returned", "")
    assert second.sink.resumes == 1 and not second.handle.interrupted
    assert first.sink.resumes == 0 and not first.handle.interrupted
    first.state.pending = 0
    first.output.recognition_completed("returned", "New direction.")
    assert first.handle.interrupted and not second.handle.interrupted
    first.handle._mark_done()
    await settle_callbacks()
    assert first.sink.resumes == second.sink.resumes == 1


@pytest.mark.asyncio
async def test_passthrough_keeps_frames_segments_and_playback_events(make_output):
    case = make_output(pending=0)
    output, sink = case.output, case.sink
    finished, started, progressed = [], [], []
    output.on("playback_finished", finished.append)
    output.on("playback_started", started.append)
    output.on("playback_progressed", progressed.append)
    frames = [SimpleNamespace(duration=.02), SimpleNamespace(duration=.02)]
    for frame in frames:
        await output.capture_frame(frame)
    output.flush()
    sink.on_playback_started(created_at=10)
    sink.on_playback_progressed(started_at=10, offset=0, duration=.04)
    sink.on_playback_finished(playback_position=.04, interrupted=False)
    result = await asyncio.wait_for(output.wait_for_playout(), 1)
    assert sink.frames == frames and sink.flushes == 1
    assert output.captured_playout_segments == 1 and not result.interrupted
    assert len(started) == len(progressed) == len(finished) == 1
    await output.capture_frame(SimpleNamespace(duration=.02))
    output.flush()
    output.clear_buffer()
    result = await asyncio.wait_for(output.wait_for_playout(), 1)
    assert result.interrupted and sink.clears == 1
    assert output.captured_playout_segments == 2 and len(finished) == 2
