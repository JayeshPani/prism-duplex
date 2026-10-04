"""Capture prerecorded speech through an existing localhost PRISM/LiveKit worker.

No models or services are started here. Received PCM and arrival times describe
client transport, not physical loudspeaker playback or benchmark timing.
"""
from __future__ import annotations

import argparse
from array import array
import asyncio
import base64
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.metadata import version
import io
import json
import math
import os
from pathlib import Path
import platform
import sys
import time
import traceback
from urllib.parse import urlparse
from uuid import uuid4
import wave

ROOT = Path(__file__).resolve().parents[1]
FRAME_MS = 20
CAPTURE_RATE = 24000
QUIET_BOUNDARY_SECONDS = .15
NON_ACK_KINDS = ("result", "response", "clarification")
REVIEWED_ROOM_SHA256 = "16843ad5a39af41cc5ba491153328d12e253459e7907b460768dd6705d15317e"


def load_wav(path: Path) -> tuple[bytes, int, bytes]:
    original = path.read_bytes()
    with wave.open(io.BytesIO(original), "rb") as wav:
        if wav.getcomptype() != "NONE" or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("input must be uncompressed mono PCM16 WAV")
        rate = wav.getframerate()
        if rate not in (16000, 24000, 48000):
            raise ValueError("input sample rate must be 16000, 24000 or 48000 Hz")
        pcm = wav.readframes(wav.getnframes())
        if not pcm or len(pcm) != wav.getnframes() * 2:
            raise ValueError("input WAV is empty or truncated")
    return pcm, rate, original


def pcm_frames(pcm: bytes, rate: int):
    if len(pcm) % 2:
        raise ValueError("PCM16 payload must contain complete samples")
    size = rate * FRAME_MS // 1000 * 2
    for offset in range(0, len(pcm), size):
        yield offset // 2, pcm[offset:offset + size]


def rms_db(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    energy = sum(v * v for v in samples) / max(1, len(samples))
    return 10 * math.log10(max(energy, 1e-12) / 32768**2)


async def publish_pcm(source, frame_factory, pcm: bytes, rate: int, record, *,
                      label: str, clock=time.monotonic, sleep=asyncio.sleep, stop_when=None) -> dict:
    """Use absolute sample deadlines so capture overhead is not added to each frame."""
    started = clock()
    published_samples = 0
    record("input_started", label=label, at_monotonic=started, samples=len(pcm) // 2)
    for sample, chunk in pcm_frames(pcm, rate):
        due = started + sample / rate
        await sleep(max(0, due - clock()))
        if stop_when is not None and stop_when():
            record("input_stopped_early", label=label, at_monotonic=clock(), published_samples=published_samples)
            break
        queued_at = clock()
        record("input_frame_started", label=label, sample_offset=sample, samples=len(chunk) // 2,
               scheduled_monotonic=due, capture_started_monotonic=queued_at)
        await source.capture_frame(frame_factory(chunk, rate))
        published_samples += len(chunk) // 2
        record("input_frame_queued", label=label, sample_offset=sample,
               capture_returned_monotonic=clock(), queued_seconds=source.queued_duration)
    await source.wait_for_playout()
    ended = clock()
    record("input_drained", label=label, at_monotonic=ended)
    return {"label": label, "started_monotonic": started, "drained_monotonic": ended,
            "scheduled_end_monotonic": started + published_samples / rate, "published_samples": published_samples}


@dataclass
class Playback:
    """Conservative event-order correlation; speech_handle has no intent/text ID."""
    followup_mode: str = "result-audio"
    ready: bool = False
    latest_intent: int | None = None
    latest_final_at: float = -math.inf
    agent_state: str = ""
    pending_say: deque = field(default_factory=deque)
    handles: dict = field(default_factory=dict)
    last_loud_at: float = -math.inf
    events: list = field(default_factory=list)

    def event(self, payload: dict, at: float) -> None:
        self.events.append(payload)
        kind, data = payload.get("type"), payload.get("data", {})
        if kind == "session_started":
            self.ready = True
        elif kind == "user_final":
            self.latest_intent, self.latest_final_at = data["intent_version"], at
        elif kind == "agent_state":
            self.agent_state = data["state"]
        elif kind == "agent_say":
            self.pending_say.append(dict(data))
        elif kind == "speech_handle":
            sid = data["speech_id"]
            if data["state"] == "queued":
                # More than one unmatched text is ambiguous; do not guess which
                # utterance produced PCM or let it trigger the correction.
                saying = self.pending_say.popleft() if len(self.pending_say) == 1 else {}
                self.pending_say.clear()
                self.handles[sid] = {**saying, "speech_id": sid, "queued_at": at,
                                     "state": "queued", "first_loud_at": None}
            else:
                self.handles.setdefault(sid, {"speech_id": sid}).update(
                    state=data["state"], ended_at=at, error=data.get("error"))

    def active_speech(self, kinds):
        active = [h for h in self.handles.values() if h["state"] == "queued"]
        if len(active) != 1 or self.pending_say or self.agent_state != "speaking":
            return None
        handle = active[0]
        if handle.get("kind") not in kinds or handle.get("intent_version") != self.latest_intent:
            return None
        return handle

    def active_result(self):
        return self.active_speech(("result",))

    def frame(self, at: float, level_db: float, threshold_db: float) -> str | None:
        if level_db < threshold_db:
            return None
        prior_loud_at, self.last_loud_at = self.last_loud_at, at
        handle = (self.active_speech(NON_ACK_KINDS) if self.followup_mode == "response-finished"
                  else self.active_result())
        if handle is not None:
            if handle["first_loud_at"] is None:
                if at - prior_loud_at < QUIET_BOUNDARY_SECONDS:
                    return None
                handle["first_loud_at"] = at
                handle["preceding_quiet_seconds"] = at - prior_loud_at if math.isfinite(prior_loud_at) else None
            handle["last_loud_at"] = at
            return handle["speech_id"]
        return None

    def correction_trigger(self, now: float) -> dict | None:
        handle = self.active_result()
        if handle and handle["first_loud_at"] is not None and now - handle["last_loud_at"] <= 0.1:
            return dict(handle)
        return None

    def followup_trigger(self, now: float, preceding_input: dict, used_speech_ids: set) -> dict | None:
        prior_intent = preceding_input["intent_before_input"]
        if (self.latest_final_at < preceding_input["timing"]["started_monotonic"]
                or self.latest_intent is None
                or (prior_intent is not None and self.latest_intent <= prior_intent)):
            return None
        if self.followup_mode == "response-finished":
            if self.pending_say or any(h["state"] == "queued" for h in self.handles.values()):
                return None
            candidates = [h for h in self.handles.values()
                          if h.get("kind") != "ack" and h.get("queued_at", -math.inf) >= self.latest_final_at]
            if len(candidates) != 1:
                return None
            handle = candidates[0]
            if (handle.get("kind") not in NON_ACK_KINDS or handle.get("intent_version") != self.latest_intent
                    or handle["state"] != "finished" or handle.get("error")
                    or handle.get("first_loud_at") is None
                    or now < max(handle["ended_at"], self.last_loud_at,
                                 preceding_input["timing"]["drained_monotonic"]) + QUIET_BOUNDARY_SECONDS):
                return None
            handle = dict(handle)
        else:
            handle = self.correction_trigger(now)
        return handle if handle and handle["speech_id"] not in used_speech_ids else None

    def result_finished(self, now: float, final_input_start: float, final_input_end: float,
                        quiet_seconds: float) -> bool:
        if self.latest_final_at < final_input_start or any(h["state"] == "queued" for h in self.handles.values()):
            return False
        return any(h.get("kind") == "result" and h.get("intent_version") == self.latest_intent
                   and h["state"] == "finished" and h.get("first_loud_at") is not None
                   and now >= max(h["ended_at"], self.last_loud_at, final_input_end) + quiet_seconds
                   for h in self.handles.values())

    def probe_outcome(self, original_speech_id: str, event_start: int) -> dict:
        original = dict(self.handles.get(original_speech_id, {}))
        current = [dict(h) for h in self.handles.values()
                   if h.get("kind") == "result" and h.get("intent_version") == self.latest_intent]
        return {"original_handle": original, "current_intent_version": self.latest_intent,
                "current_result_handles": current,
                "original_result_finished": original.get("state") == "finished",
                "current_result_finished": any(h["state"] == "finished" for h in current),
                "events_since_probe_start": list(self.events[event_start:])}


async def wait_observation(predicate, changed: asyncio.Event, deadline: float, errors: list):
    while True:
        if errors:
            raise RuntimeError(errors[-1])
        if predicate():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("observation deadline exceeded")
        changed.clear()
        try:
            await asyncio.wait_for(changed.wait(), min(0.1, remaining))
        except TimeoutError:
            pass


async def wait_input_trigger(state, changed, deadline, errors, record, label, *,
                             preceding_input=None, used_speech_ids=None):
    def current():
        return (state.followup_trigger(time.monotonic(), preceding_input, used_speech_ids)
                if preceding_input is not None else state.correction_trigger(time.monotonic()))

    record("input_trigger_wait", label=label, deadline_monotonic=deadline,
           followup_trigger_mode=state.followup_mode if preceding_input is not None else "result-audio",
           preceding_input_label=preceding_input["label"] if preceding_input else None,
           preceding_input_start=preceding_input["timing"]["started_monotonic"] if preceding_input else None,
           intent_before_preceding_input=preceding_input["intent_before_input"] if preceding_input else None)
    try:
        await wait_observation(current, changed, deadline, errors)
        trigger = current()
        if trigger is None:
            raise RuntimeError(f"substantive playback ended before {label} dispatch")
    except BaseException as error:
        record("input_trigger_failed", label=label, error=repr(error))
        raise
    record(f"{label}_trigger", evidence=trigger)
    return trigger


async def observe_probe_window(publish_tail, changed: asyncio.Event, deadline: float, errors: list) -> bool:
    """Observe through an absolute deadline, regardless of transcript/handle state."""
    tail_timeout = asyncio.timeout_at(deadline)
    try:
        async with tail_timeout:
            await publish_tail()
    except TimeoutError:
        if not tail_timeout.expired():
            raise
    await wait_observation(lambda: time.monotonic() >= deadline, changed, deadline, errors)
    return tail_timeout.expired()


def local_url(value: str) -> str:
    url = urlparse(value)
    if (url.scheme not in {"ws", "wss"} or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment):
        raise ValueError("LiveKit URL must be a localhost ws/wss address without credentials")
    return value


def new_output(path: Path) -> Path:
    output = path.resolve()
    output.mkdir(parents=True, exist_ok=False)
    return output


def input_paths(args):
    if args.followup:
        return [("initial", args.input), *((f"followup_{i}", path) for i, path in enumerate(args.followup, 1))]
    return [(label, path) for label, path in (("initial", args.input), ("correction", args.correction),
                                             ("probe", args.probe)) if path is not None]


class NativeDataIdentity:
    """Recorder-only SDK shim: retain identity across its synchronous callback."""
    native_data_identity = None

    def _on_room_event(self, event):
        previous = self.native_data_identity
        self.native_data_identity = None
        try:
            if event.WhichOneof("message") == "data_packet_received":
                packet = event.data_packet_received
                if packet.WhichOneof("value") == "user":
                    self.native_data_identity = packet.participant_identity
            return super()._on_room_event(event)
        finally:
            self.native_data_identity = previous


class AgentEventAttribution:
    """Trust only the exact transport identity resolved in this room's map."""
    def __init__(self, lookup, agent_kind, record, deliver, *, max_packets=256, max_bytes=1048576):
        self.lookup, self.agent_kind = lookup, agent_kind
        self.record, self.deliver = record, deliver
        self.max_packets, self.max_bytes = max_packets, max_bytes
        self.pending, self.pending_bytes = {}, 0
        self.packet_count, self.counts = 0, Counter()

    def disposition(self, entry, status):
        self.counts[status] += 1
        self.record("data_attribution", packet_id=entry["packet_id"], identity=entry["identity"],
                    status=status, original_received_monotonic=entry["received_monotonic"],
                    original_received_unix=entry["received_unix"])

    def promote(self, entry):
        resolved_at, resolved_unix = time.monotonic(), time.time()
        self.disposition(entry, "promoted")
        self.deliver(entry, resolved_at, resolved_unix)

    def resolve(self, identity):
        participant = self.lookup(identity)
        if participant is None or participant.identity != identity:
            return
        for entry in self.pending.pop(identity, []):
            self.pending_bytes -= len(entry["data"])
            if participant.kind == self.agent_kind:
                self.promote(entry)
            else:
                self.disposition(entry, "rejected_nonagent")

    def receive(self, packet, native_identity):
        at, unix = time.monotonic(), time.time()
        self.packet_count += 1
        participant = packet.participant
        identity = native_identity if native_identity is not None else (participant.identity if participant else None)
        data = bytes(packet.data)
        entry = dict(packet_id=self.packet_count, identity=identity, data=data,
                     received_monotonic=at, received_unix=unix)
        self.record("data_packet", packet_id=self.packet_count,
                    participant=participant.identity if participant else None,
                    native_participant_identity=native_identity, topic=packet.topic,
                    payload_base64=base64.b64encode(data).decode(),
                    received_monotonic=at, received_unix=unix)
        if packet.topic != "agent-events":
            return
        if not identity or not identity.strip():
            self.disposition(entry, "rejected_missing_identity")
            return
        # Drain older receipts for this sender before accepting its current packet.
        self.resolve(identity)
        participant = self.lookup(identity)
        if participant is not None and participant.identity == identity:
            if participant.kind == self.agent_kind:
                self.promote(entry)
            else:
                self.disposition(entry, "rejected_nonagent")
        elif (sum(map(len, self.pending.values())) >= self.max_packets
              or self.pending_bytes + len(data) > self.max_bytes):
            self.disposition(entry, "rejected_buffer_full")
        else:
            self.pending.setdefault(identity, []).append(entry)
            self.pending_bytes += len(data)
            self.disposition(entry, "buffered")

    def close(self):
        for entries in self.pending.values():
            for entry in entries:
                self.disposition(entry, "unresolved_on_close")
        self.pending.clear()
        self.pending_bytes = 0
        return dict(self.counts)


async def run(args) -> dict:
    from livekit import api, rtc

    output = new_output(args.out)
    report = {"status": "preparing", "started_at": datetime.now(timezone.utc).isoformat(),
              "command": sys.argv,
              "room": f"car-audio-{uuid4().hex}", "config": {
                  "url": args.url, "ready_timeout": args.ready_timeout, "response_timeout": args.response_timeout,
                  "probe_observation_seconds": args.probe_observation_seconds if args.probe else None,
                  "followup_trigger": args.followup_trigger,
                  "followup_finished_quiet_seconds": QUIET_BOUNDARY_SECONDS if args.followup_trigger == "response-finished" else None,
                  "quiet_seconds": args.quiet_seconds, "threshold_db": args.threshold_db,
                  "frame_ms": FRAME_MS, "capture_rate": CAPTURE_RATE, "trailing_silence_max_seconds": 1.0,
                  "result_attribution_quiet_boundary_seconds": QUIET_BOUNDARY_SECONDS},
              "packages": {name: version(name) for name in ("livekit", "livekit-api", "livekit-agents")},
              "python": sys.version, "platform": platform.platform(), "inputs": [], "tracks": [],
              "limitations": ["Frame arrival and PCM activity are client transport evidence, not physical audible timing.",
                  "PCM files preserve received frame order; frame JSONL supplies arrival timestamps and gaps. Do not treat concatenated PCM as a wall-clock timeline.",
                  "Speech attribution uses adjacent agent_say/speech_handle event order plus a client quiet boundary. Concurrent/unmatched speech is rejected, but RTP/data cross-channel attribution remains heuristic.",
                  ("Probe completion means only that the fixed observation window was captured; unfinished speech, recognition and interruptions remain outcomes, not a quality pass."
                   if args.probe else "Completion means current result handle finished plus observed client quiet tail; it is not a navigation accuracy score."),
                  "Synthetic prerecorded input has no microphone/speaker echo path; no FDB input or scoring changes.",
                  "Loopback transport does not imply air-gapped SDK behavior; default ICE/STUN services may be contacted.",
                  "Delayed data attribution requires the same native sender identity to resolve as AGENT. Coordinator receipts retain original times; attribution-resolution times are recorded separately. Unresolved, blank and non-agent senders remain untrusted.",
                  "Source hashes describe this checkout; only a provided worker receipt and observed session events describe the separate worker."]}
    state, changed, errors = Playback(followup_mode=args.followup_trigger), asyncio.Event(), []
    streams, tasks, pcm_files = [], [], []
    source = None
    class RecorderRoom(NativeDataIdentity, rtc.Room):
        pass

    room = RecorderRoom()
    report["rtc_identity_adapter"] = {
        "sdk_version": report["packages"]["livekit"],
        "sdk_room_source_sha256": sha256(Path(sys.modules[rtc.Room.__module__].__file__).read_bytes()).hexdigest(),
        "boundary": "Room._on_room_event synchronous data_received callback",
        "max_pending_packets": 256, "max_pending_bytes": 1048576}
    report["rtc_identity_adapter"]["handler_compatibility"] = (
        "reviewed_source_match" if report["rtc_identity_adapter"]["sdk_room_source_sha256"] == REVIEWED_ROOM_SHA256
        else "unsupported_source_requires_review")
    closing = False
    journal = (output / "events.jsonl").open("x")
    frames = (output / "frames.jsonl").open("x")

    def record(kind, **data):
        journal.write(json.dumps({"kind": kind, "received_monotonic": time.monotonic(),
                                  "received_unix": time.time(), **data}) + "\n")
        journal.flush()

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    async def receive(stream, publication, pcm_file, path):
        try:
            async for event in stream:
                at = time.monotonic()
                pcm = bytes(event.frame.data)
                level = rms_db(pcm)
                speech_id = state.frame(at, level, args.threshold_db)
                offset = pcm_file.tell()
                pcm_file.write(pcm)
                pcm_file.flush()
                frame_row = {"received_monotonic": at, "received_unix": time.time(),
                    "track_sid": publication.sid, "pcm_file": path.name, "offset_bytes": offset,
                    "size_bytes": len(pcm), "samples": event.frame.samples_per_channel,
                    "rms_db": level, "heuristic_result_speech_id": speech_id}
                if args.followup_trigger == "response-finished":
                    kind = state.handles[speech_id].get("kind") if speech_id is not None else None
                    frame_row.update(heuristic_speech_id=speech_id, heuristic_speech_kind=kind,
                                     heuristic_result_speech_id=speech_id if kind == "result" else None)
                frames.write(json.dumps(frame_row) + "\n")
                frames.flush()
                changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            errors.append(f"audio receiver: {error!r}")
            record("receiver_error", error=repr(error))
            changed.set()

    subscribed = set()

    @room.on("track_subscribed")
    def on_track(track, publication, participant):
        record("track_subscribed", sid=publication.sid, participant=participant.identity, track_kind=int(track.kind))
        attribution.resolve(participant.identity)
        if closing:
            return
        if (track.kind == rtc.TrackKind.KIND_AUDIO and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                and publication.sid not in subscribed):
            subscribed.add(publication.sid)
            if len(subscribed) > 1:
                errors.append("multiple agent audio tracks make playback attribution ambiguous")
            stream = rtc.AudioStream(track, sample_rate=CAPTURE_RATE, num_channels=1, frame_size_ms=FRAME_MS)
            streams.append(stream)
            path = output / f"received-{len(pcm_files) + 1}.pcm"
            pcm_file = path.open("xb")
            pcm_files.append(pcm_file)
            report["tracks"].append({"sid": publication.sid, "participant": participant.identity,
                "file": path.name, "sample_rate": CAPTURE_RATE, "channels": 1, "sample_bytes": 2})
            tasks.append(asyncio.create_task(receive(stream, publication, pcm_file, path)))
            changed.set()

    def promote_data(entry, resolved_at, resolved_unix):
        try:
            payload = json.loads(entry["data"])
            record("coordinator_event", event=payload, packet_id=entry["packet_id"],
                   participant=entry["identity"], observed_monotonic=entry["received_monotonic"],
                   received_monotonic=entry["received_monotonic"], received_unix=entry["received_unix"],
                   attribution_resolved_monotonic=resolved_at, attribution_resolved_unix=resolved_unix)
            state.event(payload, entry["received_monotonic"])
        except (ValueError, KeyError, TypeError) as error:
            errors.append(f"coordinator event could not be interpreted: {error!r}")
            record("data_error", packet_id=entry["packet_id"], error=repr(error))
        changed.set()

    attribution = AgentEventAttribution(lambda identity: room.remote_participants.get(identity),
        rtc.ParticipantKind.PARTICIPANT_KIND_AGENT, record, promote_data)

    @room.on("data_received")
    def on_data(packet):
        attribution.receive(packet, room.native_data_identity)

    @room.on("participant_connected")
    def on_participant(participant):
        record("participant_connected", participant=participant.identity, participant_kind=int(participant.kind))
        attribution.resolve(participant.identity)

    @room.on("disconnected")
    def on_disconnected(reason):
        record("disconnected", reason=str(reason), expected=closing)
        if not closing:
            errors.append(f"room disconnected: {reason}")
            changed.set()

    stage = "inputs_and_provenance"
    save()
    try:
        if report["rtc_identity_adapter"]["handler_compatibility"] != "reviewed_source_match":
            raise RuntimeError("unreviewed LiveKit Room source for native data identity adapter")
        clips = []
        for label, path in input_paths(args):
            pcm, rate, original = load_wav(path)
            if clips and rate != clips[0][2]:
                raise ValueError("all input WAVs must have the same sample rate")
            (output / f"{label}.wav").write_bytes(original)
            active = [(sample, len(chunk) // 2) for sample, chunk in pcm_frames(pcm, rate)
                      if rms_db(chunk) >= args.threshold_db]
            report["inputs"].append({"label": label, "source": str(path.resolve()), "file": f"{label}.wav",
                "sha256": sha256(original).hexdigest(), "sample_rate": rate, "samples": len(pcm) // 2,
                "duration_seconds": len(pcm) / 2 / rate,
                "activity_start_sample": active[0][0] if active else None,
                "activity_end_sample": sum(active[-1]) if active else None})
            clips.append((label, pcm, rate))
        report["source_sha256"] = {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest()
            for p in [*sorted((ROOT / "agent").rglob("*.py")), ROOT / "agent/config.yaml", Path(__file__).resolve()]}
        for name in ("worker_manifest", "protocol"):
            path = getattr(args, name)
            if path:
                data = path.read_bytes()
                (output / f"{name}.json").write_bytes(data)
                report[name] = {"source": str(path.resolve()), "sha256": sha256(data).hexdigest()}
        save()
        stage = "connect_and_session_ready"
        token = (api.AccessToken(os.environ.get("LIVEKIT_API_KEY", "devkey"), os.environ.get("LIVEKIT_API_SECRET", "secret"))
                 .with_identity(f"audio-probe-{uuid4().hex}").with_ttl(timedelta(minutes=15))
                 .with_grants(api.VideoGrants(room_join=True, room=report["room"])).to_jwt())
        async with asyncio.timeout(args.ready_timeout):
            await room.connect(args.url, token, options=rtc.RoomOptions(auto_subscribe=True))
            record("connected", room=room.name)
            for participant in room.remote_participants.values():
                attribution.resolve(participant.identity)
                for publication in participant.track_publications.values():
                    if publication.track:
                        on_track(publication.track, publication, participant)
            source = rtc.AudioSource(clips[0][2], 1, queue_size_ms=100)
            track = rtc.LocalAudioTrack.create_audio_track("prerecorded-input", source)
            publication = await room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            await wait_observation(lambda: state.ready, changed, time.monotonic() + args.ready_timeout, errors)
            await publication.wait_for_subscription()
            record("input_track_subscribed", sid=publication.sid)
        record("ready", subscribed_agent_tracks=sorted(subscribed))
        report["status"] = "running"
        save()
        make_frame = lambda pcm, rate: rtc.AudioFrame(pcm, rate, 1, len(pcm) // 2)
        used_speech_ids = set()
        for index, (label, pcm, rate) in enumerate(clips):
            stage = f"{label}_trigger_wait" if index else f"{label}_input"
            async with asyncio.timeout(args.response_timeout):
                if index:
                    trigger = await wait_input_trigger(state, changed, time.monotonic() + args.response_timeout,
                        errors, record, label, preceding_input=report["inputs"][index - 1] if args.followup else None,
                        used_speech_ids=used_speech_ids)
                    report[f"{label}_trigger"] = trigger
                    used_speech_ids.add(trigger["speech_id"])
                    if label == "probe":
                        probe_event_start = len(state.events)
                stage = f"{label}_input"
                report["inputs"][index]["intent_before_input"] = state.latest_intent
                timing = await publish_pcm(source, make_frame, pcm, rate, record, label=label)
                report["inputs"][index]["timing"] = timing
                # Separate from the actual input file and its measured speech activity.
                if label != "probe":
                    stage = f"{label}_trailing_silence"
                    next_trigger = None
                    if args.followup and index < len(clips) - 1:
                        next_trigger = lambda: state.followup_trigger(time.monotonic(), report["inputs"][index], used_speech_ids)
                    elif (args.correction or args.probe) and index == 0:
                        next_trigger = lambda: state.correction_trigger(time.monotonic())
                    await publish_pcm(source, make_frame, bytes(rate * 2), rate, record, label=f"{label}_trailing_silence",
                                      stop_when=next_trigger)
                save()
        if args.probe:
            stage = "probe_observation"
            probe_end = report["inputs"][-1]["timing"]["drained_monotonic"]
            deadline = probe_end + args.probe_observation_seconds
            report["probe_observation"] = {"started_monotonic": probe_end, "deadline_monotonic": deadline}
            record("probe_observation_started", **report["probe_observation"])
            clipped = await observe_probe_window(
                lambda: publish_pcm(source, make_frame, bytes(rate * 2), rate, record, label="probe_trailing_silence"),
                changed, deadline, errors)
            report["probe_observation"].update(
                observed_end_monotonic=time.monotonic(), trailing_silence_clipped_at_deadline=clipped,
                **state.probe_outcome(report["probe_trigger"]["speech_id"], probe_event_start))
            record("probe_observation_finished", observation=report["probe_observation"])
            report["status"] = "completed_probe_observation"
        else:
            stage = "final_result_completion"
            final_input = report["inputs"][-1]["timing"]
            await wait_observation(lambda: state.result_finished(time.monotonic(), final_input["started_monotonic"],
                                   final_input["drained_monotonic"], args.quiet_seconds), changed,
                                   time.monotonic() + args.response_timeout, errors)
            report["status"] = "completed_observation"
    except BaseException as error:
        report.update(status="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                      failed_stage=stage, error=repr(error), traceback=traceback.format_exc())
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        closing = True
        if source:
            source.clear_queue()
        cleanup = [room.disconnect(), *(s.aclose() for s in streams)]
        if source:
            cleanup.append(source.aclose())
        try:
            results = await asyncio.wait_for(asyncio.gather(*cleanup, return_exceptions=True), 10)
            report["cleanup_errors"] = [repr(r) for r in results if isinstance(r, BaseException)]
        except BaseException as error:
            report["cleanup_errors"] = [repr(error)]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        report["data_attribution_counts"] = attribution.close()
        report.update(finished_at=datetime.now(timezone.utc).isoformat(),
                      event_counts=dict(Counter(e.get("type") for e in state.events)),
                      speech_handles=list(state.handles.values()), receiver_errors=errors)
        for file in pcm_files:
            file.close()
        frames.close()
        journal.close()
        report["artifact_sha256"] = {p.name: sha256(p.read_bytes()).hexdigest()
                                     for p in output.iterdir() if p.is_file() and p.name != "report.json"}
        save()
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    second_input = parser.add_mutually_exclusive_group()
    second_input.add_argument("--correction", type=Path)
    second_input.add_argument("--probe", type=Path, help="Inject during result audio; observe without assuming a new transcript")
    second_input.add_argument("--followup", type=Path, action="append",
                              help="Repeat for ordered turns, each during the preceding input's new result audio")
    parser.add_argument("--followup-trigger", choices=("result-audio", "response-finished"), default="result-audio",
                        help="With --followup, start during result PCM (default) or after a fresh non-ack reply finishes and 150ms quiet")
    parser.add_argument("--probe-observation-seconds", type=float, default=12,
                        help="Fixed window after probe input drains, independent of speech completion (default: 12)")
    parser.add_argument("--out", type=Path, required=True, help="New directory; existing output is refused")
    parser.add_argument("--url", default=os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880"))
    parser.add_argument("--worker-manifest", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--ready-timeout", type=float, default=180)
    parser.add_argument("--response-timeout", type=float, default=120)
    parser.add_argument("--quiet-seconds", type=float, default=1)
    parser.add_argument("--threshold-db", type=float, default=-40)
    args = parser.parse_args(argv)
    if args.followup_trigger != "result-audio" and not args.followup:
        parser.error("--followup-trigger response-finished requires --followup")
    try:
        args.url = local_url(args.url)
        if any(not math.isfinite(v) or v <= 0 for v in (
                args.ready_timeout, args.response_timeout, args.quiet_seconds, args.probe_observation_seconds)):
            raise ValueError("deadlines and quiet tail must be finite and positive")
        if not math.isfinite(args.threshold_db) or not -100 <= args.threshold_db <= 0:
            raise ValueError("threshold must be between -100 and 0 dBFS")
    except ValueError as error:
        parser.error(str(error))
    return args


def main() -> int:
    args = parse_args()
    report = asyncio.run(run(args))
    print(json.dumps({"status": report["status"], "room": report["room"], "output": str(args.out.resolve())}))
    return 0 if report["status"] in {"completed_observation", "completed_probe_observation"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
