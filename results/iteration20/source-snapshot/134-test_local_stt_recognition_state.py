"""Room recognition notifications with controlled awaitables and no model loads."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("livekit.agents")
from livekit import rtc
from livekit.agents.types import APIConnectOptions

from agent.pipeline import local_stt


@pytest.fixture
def recognizer(monkeypatch):
    started = asyncio.Queue()

    async def transcribe(pcm):
        native = asyncio.get_running_loop().create_future()
        started.put_nowait(native)
        # Model native work may outlive cancellation of the awaiting room task.
        return await asyncio.shield(native)

    monkeypatch.setattr(local_stt._Engine, "get", lambda *args: SimpleNamespace(transcribe=transcribe))
    stt = local_stt.LocalSTT("test", "test")
    notifications = []
    stt.on("recognition_completed", lambda status, text: notifications.append(
        (status, text, stt.pending_recognitions)))
    return stt, started, notifications


def recognize(stt):
    frame = rtc.AudioFrame(b"\0" * 3200, sample_rate=16000, num_channels=1,
                           samples_per_channel=1600)
    return asyncio.create_task(stt._recognize_impl(frame, conn_options=APIConnectOptions()))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Stop navigation.", "", "   "])
async def test_completed_result_is_reported_after_pending_count_decreases(recognizer, text):
    stt, started, notifications = recognizer
    task = recognize(stt)
    native = await started.get()
    assert stt.pending_recognitions == 1 and notifications == []
    native.set_result(text)
    event = await task
    assert event.alternatives[0].text == text
    assert notifications == [("returned", text, 0)]
    assert stt.pending_recognitions == 0


@pytest.mark.asyncio
async def test_overlapping_awaits_do_not_report_the_room_idle_early(recognizer):
    stt, started, notifications = recognizer
    first, second = recognize(stt), recognize(stt)
    first_native, second_native = await started.get(), await started.get()
    assert stt.pending_recognitions == 2
    second_native.set_result("")
    await second
    assert notifications == [("returned", "", 1)]
    first_native.set_result("Cancel.")
    await first
    assert notifications == [("returned", "", 1), ("returned", "Cancel.", 0)]


@pytest.mark.asyncio
async def test_recognition_failure_is_not_reported_as_empty_speech(recognizer):
    stt, started, notifications = recognizer
    task = recognize(stt)
    native = await started.get()
    native.set_exception(RuntimeError("recognition failed"))
    with pytest.raises(RuntimeError, match="recognition failed"):
        await task
    assert notifications == [("failed", None, 0)]
    assert stt.pending_recognitions == 0


@pytest.mark.asyncio
async def test_cancelled_await_does_not_claim_native_completion(recognizer):
    stt, started, notifications = recognizer
    task = recognize(stt)
    native = await started.get()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert notifications == [("cancelled", None, 0)]
    assert not native.done()
    native.set_result("Late result.")
    await asyncio.sleep(0)
    assert notifications == [("cancelled", None, 0)]


@pytest.mark.asyncio
async def test_whole_span_retranscription_is_not_an_unresolved_input_segment(recognizer):
    stt, started, notifications = recognizer
    stt._segments.extend([(1.0, local_stt.np.zeros(16, dtype=local_stt.np.int16)),
                          (2.0, local_stt.np.zeros(16, dtype=local_stt.np.int16))])
    task = asyncio.create_task(stt.transcribe_since(0))
    native = await started.get()
    assert stt.pending_recognitions == 0
    native.set_result("The combined request.")
    assert await task == "The combined request."
    assert notifications == []
