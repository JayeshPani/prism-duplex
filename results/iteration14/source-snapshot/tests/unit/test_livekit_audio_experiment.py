"""Capture tests: no RTC connection, worker or models; one optional SDK boundary check."""
import asyncio
import json
import struct
import time
from types import SimpleNamespace
import wave

import pytest

import scripts.local_livekit_audio_experiment as capture
from scripts.local_livekit_audio_experiment import (
    Playback, load_wav, local_url, new_output, pcm_frames, publish_pcm, rms_db, wait_observation,
)


def event(playback, event_type, at=0, **data):
    playback.event({"type": event_type, "data": data}, at)


def speaking_result():
    state = Playback()
    event(state, "user_final", 1, intent_version=1)
    event(state, "agent_say", 2, kind="result", intent_version=1, text="Route ready.")
    event(state, "speech_handle", 2.1, speech_id="result1", state="queued")
    event(state, "agent_state", 2.2, state="speaking")
    return state


def test_wav_framing_preserves_tail_and_activity_without_resampling(tmp_path):
    path = tmp_path / "speech.wav"
    pcm = struct.pack("<485h", *([0] * 320 + [3276] * 165))
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    loaded, rate, original = load_wav(path)
    chunks = list(pcm_frames(loaded, rate))
    assert original == path.read_bytes() and loaded == pcm
    assert [(offset, len(chunk) // 2) for offset, chunk in chunks] == [(0, 320), (320, 165)]
    assert b"".join(chunk for _, chunk in chunks) == pcm
    assert rms_db(chunks[0][1]) < -100
    assert rms_db(chunks[1][1]) == pytest.approx(-20, abs=.01)


@pytest.mark.parametrize("channels,width,rate", [(2, 2, 16000), (1, 1, 16000), (1, 2, 8000)])
def test_unsupported_input_is_rejected(tmp_path, channels, width, rate):
    path = tmp_path / "bad.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(channels * width * 10))
    with pytest.raises(ValueError):
        load_wav(path)


@pytest.mark.asyncio
async def test_absolute_frame_schedule_does_not_accumulate_capture_overhead():
    now, starts, records = [0.0], [], []

    async def sleep(delay):
        now[0] += delay

    class Source:
        queued_duration = .02

        async def capture_frame(self, frame):
            starts.append(now[0])
            now[0] += .007

        async def wait_for_playout(self):
            now[0] = max(now[0], .06)

    result = await publish_pcm(Source(), lambda pcm, rate: pcm, bytes(16000 * 2 * 60 // 1000), 16000,
                              lambda kind, **data: records.append((kind, data)), label="initial",
                              clock=lambda: now[0], sleep=sleep)
    assert starts == pytest.approx([0, .02, .04])
    assert result["drained_monotonic"] == .06
    assert [kind for kind, _ in records].count("input_frame_started") == 3


@pytest.mark.asyncio
async def test_failed_frame_attempt_is_retained():
    records = []

    class Source:
        async def capture_frame(self, frame):
            raise RuntimeError("closed source")

    with pytest.raises(RuntimeError, match="closed source"):
        await publish_pcm(Source(), lambda pcm, rate: pcm, bytes(640), 16000,
                          lambda kind, **data: records.append((kind, data)), label="initial")
    assert [kind for kind, _ in records] == ["input_started", "input_frame_started"]


@pytest.mark.asyncio
async def test_trailing_silence_stops_for_trigger_before_next_pcm_write():
    chunks, records = [], []

    class Source:
        queued_duration = 0

        async def capture_frame(self, frame):
            chunks.append(frame)

        async def wait_for_playout(self):
            records.append(("drained", {}))

    result = await publish_pcm(Source(), lambda pcm, rate: pcm, bytes(32000), 16000,
                              lambda kind, **data: records.append((kind, data)), label="silence",
                              stop_when=lambda: len(chunks) == 1)
    assert len(chunks) == 1 and result["published_samples"] == 320
    assert [kind for kind, _ in records][-3:] == ["input_stopped_early", "drained", "input_drained"]


def test_acknowledgment_audio_or_silent_result_cannot_trigger_correction():
    state = Playback()
    event(state, "user_final", intent_version=1)
    event(state, "agent_say", kind="ack", intent_version=1, text="One moment.")
    event(state, "speech_handle", speech_id="ack", state="queued")
    event(state, "agent_state", state="speaking")
    state.frame(1, -10, -40)
    assert state.correction_trigger(1) is None
    result = speaking_result()
    result.frame(3, -80, -40)
    assert result.correction_trigger(3) is None


def test_correction_requires_recent_result_audio_and_rechecks_finished_or_stale_handle():
    state = speaking_result()
    assert state.frame(3, -10, -40) == "result1"
    assert state.correction_trigger(3.01)["kind"] == "result"
    assert state.correction_trigger(3.2) is None
    event(state, "speech_handle", 3.03, speech_id="result1", state="finished")
    assert state.correction_trigger(3.04) is None
    state = speaking_result()
    state.frame(3, -10, -40)
    event(state, "user_final", 3.01, intent_version=2)
    assert state.correction_trigger(3.02) is None


def test_overlapping_ack_or_ambiguous_event_order_cannot_trigger():
    state = speaking_result()
    event(state, "agent_say", kind="ack", intent_version=1)
    event(state, "speech_handle", speech_id="ack", state="queued")
    state.frame(3, -10, -40)
    assert state.correction_trigger(3) is None
    state = Playback()
    event(state, "user_final", intent_version=1)
    event(state, "agent_say", kind="ack", intent_version=1)
    event(state, "agent_say", kind="result", intent_version=1)
    event(state, "speech_handle", speech_id="ambiguous", state="queued")
    event(state, "agent_state", state="speaking")
    state.frame(3, -10, -40)
    assert state.correction_trigger(3) is None


def test_immediate_late_ack_tail_cannot_establish_result_audio():
    state = speaking_result()
    state.last_loud_at = 2.9
    assert state.frame(3, -10, -40) is None
    assert state.correction_trigger(3) is None
    assert state.frame(3.3, -10, -40) == "result1"
    assert state.correction_trigger(3.3)["preceding_quiet_seconds"] == pytest.approx(.3)


def test_completion_waits_for_current_result_audio_and_client_quiet_tail():
    state = speaking_result()
    state.frame(3, -10, -40)
    event(state, "speech_handle", 4, speech_id="result1", state="finished")
    assert not state.result_finished(4.2, .5, 2, 1)
    state.frame(4.5, -10, -40)  # audio can arrive after server-side handle completion
    assert not state.result_finished(5.2, .5, 2, 1)
    assert state.result_finished(5.6, .5, 2, 1)
    assert not state.result_finished(5.6, 4, 5, 1)  # no finalized correction yet
    event(state, "user_final", 5, intent_version=2)
    assert not state.result_finished(7, 4, 5, 1)  # old result cannot finish the new turn


@pytest.mark.asyncio
async def test_wait_has_deadline_and_receiver_errors_override_completion():
    with pytest.raises(TimeoutError):
        await wait_observation(lambda: False, asyncio.Event(), time.monotonic() + .01, [])
    with pytest.raises(RuntimeError, match="receiver stopped"):
        await wait_observation(lambda: True, asyncio.Event(), time.monotonic() + 1, ["receiver stopped"])


@pytest.mark.parametrize("url", ["wss://example.com", "https://localhost", "ws://user:secret@localhost", "ws://localhost?token=secret"])
def test_local_url_rejects_remote_or_credential_bearing_destinations(url):
    with pytest.raises(ValueError):
        local_url(url)


def test_existing_output_is_never_overwritten(tmp_path):
    existing = tmp_path / "run"
    existing.mkdir()
    marker = existing / "report.json"
    marker.write_text("existing evidence")
    with pytest.raises(FileExistsError):
        new_output(existing)
    assert marker.read_text() == "existing evidence"


def test_probe_cli_is_optional_and_mutually_exclusive_with_correction():
    base = ["--input", "initial.wav", "--out", "new-run"]
    baseline = capture.parse_args(base)
    assert baseline.probe is None and baseline.correction is None
    correction = capture.parse_args(base + ["--correction", "correction.wav"])
    assert correction.probe is None and str(correction.correction) == "correction.wav"
    probe = capture.parse_args(base + ["--probe", "noise.wav"])
    assert str(probe.probe) == "noise.wav" and probe.correction is None
    assert probe.probe_observation_seconds == 12
    with pytest.raises(SystemExit):
        capture.parse_args(base + ["--probe", "noise.wav", "--correction", "correction.wav"])


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf", "-inf"])
def test_probe_cli_rejects_invalid_observation_duration(seconds):
    with pytest.raises(SystemExit):
        capture.parse_args(["--input", "initial.wav", "--probe", "noise.wav", "--out", "new-run",
                            f"--probe-observation-seconds={seconds}"])


@pytest.mark.asyncio
async def test_probe_observes_full_window_despite_new_transcript_and_failed_result():
    state = speaking_result()
    state.frame(3, -10, -40)
    trigger = state.correction_trigger(3)
    event_start = len(state.events)
    deadline = time.monotonic() + .025

    async def short_tail():
        # A new transcript and failed original handle are outcomes, not a reason
        # to reset the observation window or claim successful speech completion.
        event(state, "user_final", 4, intent_version=2, text="invented recognition")
        event(state, "interruption_requested", 4.1, reason="new turn")
        event(state, "speech_handle", 4.2, speech_id="result1", state="failed", error="synthesis failed")

    clipped = await capture.observe_probe_window(short_tail, asyncio.Event(), deadline, [])
    assert not clipped and time.monotonic() >= deadline
    outcome = state.probe_outcome(trigger["speech_id"], event_start)
    assert outcome["original_handle"]["state"] == "failed"
    assert not outcome["original_result_finished"] and not outcome["current_result_finished"]
    assert outcome["current_intent_version"] == 2
    assert [row["type"] for row in outcome["events_since_probe_start"]] == [
        "user_final", "interruption_requested", "speech_handle"]
    assert outcome["events_since_probe_start"][-1]["data"]["error"] == "synthesis failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", [False, True])
async def test_probe_with_no_new_events_waits_window_and_reports_original_result_state(finished):
    state = speaking_result()
    state.frame(3, -10, -40)
    if finished:
        event(state, "speech_handle", 4, speech_id="result1", state="finished")
    event_start = len(state.events)
    deadline = time.monotonic() + .015

    async def no_tail():
        pass

    await capture.observe_probe_window(no_tail, asyncio.Event(), deadline, [])
    assert time.monotonic() >= deadline
    outcome = state.probe_outcome("result1", event_start)
    assert outcome["original_result_finished"] is finished
    assert outcome["current_result_finished"] is finished
    assert outcome["events_since_probe_start"] == []


@pytest.mark.asyncio
async def test_probe_deadline_caps_trailing_silence_instead_of_extending_observation():
    cancelled = asyncio.Event()

    async def blocked_tail():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    deadline = time.monotonic() + .01
    clipped = await asyncio.wait_for(
        capture.observe_probe_window(blocked_tail, asyncio.Event(), deadline, []), .5)
    assert clipped and cancelled.is_set() and time.monotonic() >= deadline


@pytest.mark.asyncio
async def test_probe_preserves_source_and_receiver_failures():
    async def failed_tail():
        raise TimeoutError("source transport timed out")

    with pytest.raises(TimeoutError, match="source transport timed out"):
        await capture.observe_probe_window(failed_tail, asyncio.Event(), time.monotonic() + 1, [])

    async def no_tail():
        pass

    with pytest.raises(RuntimeError, match="receiver stopped"):
        await capture.observe_probe_window(no_tail, asyncio.Event(), time.monotonic(), ["receiver stopped"])


def attribution_probe(monkeypatch, **bounds):
    participants, records, delivered, now = {}, [], [], [1.0]
    playback = Playback()
    monkeypatch.setattr(capture.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(capture.time, "time", lambda: now[0] + 1000)

    def deliver(entry, resolved_at, resolved_unix):
        delivered.append({**entry, "resolved_at": resolved_at, "resolved_unix": resolved_unix})
        playback.event(json.loads(entry["data"]), entry["received_monotonic"])

    attribution = capture.AgentEventAttribution(participants.get, 4,
        lambda kind, **data: records.append({"kind": kind, **data}), deliver, **bounds)
    return attribution, participants, records, delivered, playback, now


def data_packet(event_type, *, participant=None, **data):
    return SimpleNamespace(data=json.dumps({"type": event_type, "data": data}).encode(),
                           topic="agent-events", participant=participant)


def test_native_data_before_participant_promotes_ready_and_listening_at_original_receipt(monkeypatch):
    attribution, participants, records, delivered, playback, now = attribution_probe(monkeypatch)
    attribution.receive(data_packet("agent_state", state="listening"), "agent-a")
    now[0] = 2.0
    attribution.receive(data_packet("session_started"), "agent-a")
    participants["agent-b"] = SimpleNamespace(identity="agent-b", kind=4)
    attribution.resolve("agent-b")
    assert not playback.ready and not playback.events
    participants["agent-a"] = SimpleNamespace(identity="agent-a", kind=4)
    now[0] = 3.0
    attribution.resolve("agent-a")
    assert playback.ready and playback.agent_state == "listening"
    assert [e["type"] for e in playback.events] == ["agent_state", "session_started"]
    assert [e["received_monotonic"] for e in delivered] == [1, 2]
    assert [e["received_unix"] for e in delivered] == [1001, 1002]
    assert [e["resolved_at"] for e in delivered] == [3, 3]
    assert [e["resolved_unix"] for e in delivered] == [1003, 1003]
    raw = [r for r in records if r["kind"] == "data_packet"]
    assert [r["participant"] for r in raw] == [None, None]
    assert [r["native_participant_identity"] for r in raw] == ["agent-a", "agent-a"]
    assert attribution.close() == {"buffered": 2, "promoted": 2}


@pytest.mark.parametrize("identity", [None, "", "   ", "unknown", "human"])
def test_untrusted_sender_cannot_become_ready_from_payload_or_sole_agent(monkeypatch, identity):
    attribution, participants, records, delivered, playback, _ = attribution_probe(monkeypatch)
    participants["sole-agent"] = SimpleNamespace(identity="sole-agent", kind=4)
    participants["human"] = SimpleNamespace(identity="human", kind=0)
    attribution.receive(data_packet("session_started", identity="sole-agent", kind="agent"), identity)
    attribution.resolve("sole-agent")
    attribution.resolve("human")
    attribution.close()
    assert not playback.ready and not delivered
    assert any(r.get("status") in {"rejected_missing_identity", "rejected_nonagent", "unresolved_on_close"}
               for r in records)


def test_native_identity_overrides_other_public_participant_and_nonagent_resolution_rejects(monkeypatch):
    attribution, participants, _, delivered, playback, _ = attribution_probe(monkeypatch)
    agent = SimpleNamespace(identity="agent", kind=4)
    participants["agent"] = agent
    attribution.receive(data_packet("session_started", participant=agent), "sender")
    participants["sender"] = SimpleNamespace(identity="sender", kind=0)
    attribution.resolve("sender")
    # An explicitly empty native identity stays untrusted even if a public object exists.
    attribution.receive(data_packet("session_started", participant=agent), "")
    assert not playback.ready and not delivered
    assert attribution.close() == {"buffered": 1, "rejected_nonagent": 1, "rejected_missing_identity": 1}


def test_sender_fifo_drain_precedes_later_receipt_and_is_exactly_once(monkeypatch):
    attribution, participants, _, delivered, playback, now = attribution_probe(monkeypatch)
    first = data_packet("agent_state", state="listening")
    first.data = bytearray(first.data)
    attribution.receive(first, "agent")
    first.data[:] = b"corrupted after callback"
    now[0] = 2
    attribution.receive(data_packet("session_started"), "agent")
    participants["agent"] = SimpleNamespace(identity="agent", kind=4)
    now[0] = 3
    attribution.receive(data_packet("agent_state", state="speaking"), "agent")
    attribution.resolve("agent")
    attribution.resolve("agent")
    assert [e["packet_id"] for e in delivered] == [1, 2, 3]
    assert [e["received_monotonic"] for e in delivered] == [1, 2, 3]
    assert playback.ready and playback.agent_state == "speaking"
    assert len(playback.events) == 3 and attribution.pending_bytes == 0


@pytest.mark.parametrize("bounds", [{"max_packets": 1}, {"max_bytes": 50}])
def test_unresolved_buffers_are_bounded_and_explicit_without_blocking_known_agent(monkeypatch, bounds):
    attribution, participants, records, _, playback, _ = attribution_probe(monkeypatch, **bounds)
    attribution.receive(data_packet("session_started"), "unknown-a")
    attribution.receive(data_packet("session_started"), "unknown-b")
    assert not playback.ready
    participants["agent"] = SimpleNamespace(identity="agent", kind=4)
    attribution.receive(data_packet("session_started"), "agent")
    assert playback.ready
    counts = attribution.close()
    assert counts == {"buffered": 1, "rejected_buffer_full": 1, "promoted": 1, "unresolved_on_close": 1}
    assert not attribution.pending and attribution.pending_bytes == 0
    assert len([r for r in records if r["kind"] == "data_packet"]) == 3


def native_event(identity, **extra):
    packet = SimpleNamespace(participant_identity=identity, WhichOneof=lambda _: "user", **extra)
    return SimpleNamespace(WhichOneof=lambda _: "data_packet_received", data_packet_received=packet)


def test_identity_shim_delegates_once_and_restores_after_nested_event_and_exception():
    seen = []
    failure = ValueError("SDK handler failure")

    class SDKRoom:
        def _on_room_event(self, event):
            seen.append(self.native_data_identity)
            if self.native_data_identity == "outer":
                self._on_room_event(native_event("inner"))
                assert self.native_data_identity == "outer"
            if self.native_data_identity == "broken":
                raise failure
            return "original return"

    class Room(capture.NativeDataIdentity, SDKRoom):
        pass

    room = Room()
    assert room._on_room_event(native_event("outer")) == "original return"
    assert room.native_data_identity is None and seen == ["outer", "inner"]
    with pytest.raises(ValueError) as caught:
        room._on_room_event(native_event("broken"))
    assert caught.value is failure and room.native_data_identity is None


def test_installed_sdk_handler_retains_native_copy_disposal_and_sync_callback(monkeypatch):
    # Invoke the installed handler without constructing a Room/FFI client or connecting.
    import ctypes
    sdk = pytest.importorskip("livekit.rtc.room")

    data = ctypes.create_string_buffer(b'{"type":"session_started","data":{}}')
    disposed, callbacks = [], []
    monkeypatch.setattr(sdk, "FfiHandle", lambda identifier: SimpleNamespace(dispose=lambda: disposed.append(identifier)))

    class SDKBoundary:
        _on_room_event = sdk.Room._on_room_event

        def _retrieve_remote_participant(self, identity):
            assert identity == "not-yet-in-map"
            return None

        def emit(self, name, packet):
            callbacks.append((name, packet, self.native_data_identity))

    class Room(capture.NativeDataIdentity, SDKBoundary):
        pass

    room = Room()
    owned = SimpleNamespace(handle=SimpleNamespace(id=77),
        data=SimpleNamespace(data_ptr=ctypes.addressof(data), data_len=len(data.value)))
    event = native_event("not-yet-in-map", kind=0, user=SimpleNamespace(data=owned, topic="agent-events"))
    room._on_room_event(event)
    assert disposed == [77] and len(callbacks) == 1
    name, packet, identity = callbacks[0]
    assert name == "data_received" and packet.data == data.value and packet.participant is None
    assert identity == "not-yet-in-map" and room.native_data_identity is None


def test_followup_cli_preserves_order_labels_and_legacy_inputs():
    base = ["--input", "airport.wav", "--out", "new-run"]
    args = capture.parse_args(base + ["--followup", "office.wav", "--followup", "airport-again.wav"])
    assert args.correction is None and args.probe is None
    assert [(label, str(path)) for label, path in capture.input_paths(args)] == [
        ("initial", "airport.wav"), ("followup_1", "office.wav"), ("followup_2", "airport-again.wav")]
    cancel = capture.parse_args(base + ["--followup", "cancel.wav"])
    assert [label for label, _ in capture.input_paths(cancel)] == ["initial", "followup_1"]
    legacy = capture.parse_args(base + ["--correction", "office.wav"])
    assert legacy.followup is None
    assert [label for label, _ in capture.input_paths(legacy)] == ["initial", "correction"]
    assert [label for label, _ in capture.input_paths(capture.parse_args(base))] == ["initial"]


@pytest.mark.parametrize("other", ["--correction", "--probe"])
@pytest.mark.parametrize("followup_first", [False, True])
def test_followup_cli_is_mutually_exclusive_in_either_order(other, followup_first):
    options = ["--followup", "later.wav", other, "other.wav"]
    if not followup_first:
        options = options[2:] + options[:2]
    with pytest.raises(SystemExit):
        capture.parse_args(["--input", "initial.wav", "--out", "new-run", *options])


@pytest.mark.asyncio
async def test_two_followup_waits_require_new_final_and_distinct_current_result(monkeypatch):
    state, now, records, changed = speaking_result(), [3.0], [], asyncio.Event()
    monkeypatch.setattr(capture.time, "monotonic", lambda: now[0])
    record = lambda kind, **data: records.append({"kind": kind, **data})
    used = set()
    state.frame(3, -10, -40)
    initial = {"label": "initial", "intent_before_input": None, "timing": {"started_monotonic": .5}}
    first = await capture.wait_input_trigger(state, changed, 100, [], record, "followup_1",
        preceding_input=initial, used_speech_ids=used)
    used.add(first["speech_id"])
    office = {"label": "followup_1", "intent_before_input": 1, "timing": {"started_monotonic": 3.01}}
    third_turn = asyncio.create_task(capture.wait_input_trigger(state, changed, 100, [], record, "followup_2",
        preceding_input=office, used_speech_ids=used))
    await asyncio.sleep(0)
    assert not third_turn.done()  # Old airport audio alone cannot send the return-to-airport clip.
    now[0] = 4
    event(state, "user_final", 4, intent_version=2)
    changed.set()
    await asyncio.sleep(0)
    assert not third_turn.done()  # A new final with only the old queued handle is insufficient.
    event(state, "speech_handle", 4.05, speech_id="result1", state="interruption_requested")
    event(state, "agent_say", 4.1, kind="ack", intent_version=2)
    event(state, "speech_handle", 4.1, speech_id="ack2", state="queued")
    state.frame(4.2, -10, -40)
    assert state.followup_trigger(4.2, office, used) is None
    event(state, "speech_handle", 4.25, speech_id="ack2", state="finished")
    event(state, "agent_say", 4.3, kind="result", intent_version=2, text="Office navigation started.")
    event(state, "speech_handle", 4.3, speech_id="result2", state="queued")
    assert state.followup_trigger(4.3, office, used) is None  # No substantive PCM yet.
    now[0] = 4.5
    state.frame(now[0], -10, -40)
    changed.set()
    second = await asyncio.wait_for(third_turn, .5)
    used.add(second["speech_id"])
    assert [first["speech_id"], second["speech_id"]] == ["result1", "result2"]
    assert state.followup_trigger(now[0], office, used) is None
    assert [(r["kind"], r.get("label")) for r in records] == [
        ("input_trigger_wait", "followup_1"), ("followup_1_trigger", None),
        ("input_trigger_wait", "followup_2"), ("followup_2_trigger", None)]
    assert records[2]["preceding_input_start"] == 3.01
    assert records[2]["intent_before_preceding_input"] == 1


def test_followup_rejects_late_old_final_even_with_new_speech_id():
    state = speaking_result()
    preceding = {"label": "followup_1", "intent_before_input": 1, "timing": {"started_monotonic": 3}}
    event(state, "user_final", 3.1, intent_version=1)
    event(state, "speech_handle", 3.2, speech_id="result1", state="finished")
    event(state, "agent_say", 3.3, kind="result", intent_version=1)
    event(state, "speech_handle", 3.3, speech_id="late-old-result", state="queued")
    state.frame(3.5, -10, -40)
    assert state.correction_trigger(3.5) is not None  # Legacy behavior stays available.
    assert state.followup_trigger(3.5, preceding, {"result1"}) is None


@pytest.mark.asyncio
async def test_followup_timeout_records_which_wait_failed_without_a_trigger(monkeypatch):
    state, records = speaking_result(), []
    preceding = {"label": "followup_1", "intent_before_input": 1, "timing": {"started_monotonic": 3}}

    async def expired(predicate, changed, deadline, errors):
        assert predicate() is None
        raise TimeoutError("no new substantive result")

    monkeypatch.setattr(capture, "wait_observation", expired)
    with pytest.raises(TimeoutError, match="no new substantive result"):
        await capture.wait_input_trigger(state, asyncio.Event(), 4, [],
            lambda kind, **data: records.append({"kind": kind, **data}), "followup_2",
            preceding_input=preceding, used_speech_ids={"result1"})
    assert [r["kind"] for r in records] == ["input_trigger_wait", "input_trigger_failed"]
    assert all(r["label"] == "followup_2" for r in records)
    assert "TimeoutError" in records[-1]["error"]
