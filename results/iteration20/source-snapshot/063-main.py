"""PRISM Duplex agent: LiveKit entrypoint.

    python -m agent.main start     # worker; auto-joins every new room
    python -m agent.main dev       # same, with reload

Mode per room: "car-*" uses the navigation manifest; "web-*" uses Assistant
mode with spoken results; other rooms use benchmark mode. MODE=car|assistant|bench
overrides room selection.

Local-profile inference: Silero VAD -> Parakeet STT -> our Coordinator
(resolver LLM + commit gate + executor + ledger) -> Kokoro TTS. There is no
LiveKit LLM node: every user turn is handed to the coordinator, which decides
what to say and which tools to run. LiveKit audio transport may use the cloud.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, JobExecutorType, llm
from livekit.plugins import silero  # plugins must be imported on the main thread

from .config import ROOT, load_config, make_coordinator_config, make_llm, room_mode, local_turn_handling
from .coordinator import events as E
from .coordinator.coordinator import Coordinator
from .coordinator.events import EventBus
from .coordinator.executor import Executor
from .coordinator.ledger import Ledger
from .coordinator.resolver import IntentResolver
from .coordinator.responder import Responder
from .pipeline.local_stt import LocalSTT
from .pipeline.local_tts import KokoroTTS
from .pipeline.recognition_pause import RecognitionAwareAudioOutput
from .telemetry import fdb_logger
from .telemetry.trace_writer import TraceWriter

log = logging.getLogger("prism")
CFG = load_config()
TRACE_DIR = Path(os.getenv("PRISM_TRACE_DIR", ROOT / "results" / "traces"))

# Models are loaded once per worker process and shared by all rooms (thread executor).
_STT: LocalSTT | None = None
_TTS: KokoroTTS | None = None
_VAD = None


def prewarm(_proc: agents.JobProcess | None = None) -> None:
    global _STT, _TTS, _VAD
    if _VAD is None:
        _VAD = silero.VAD.load(min_speech_duration=0.05, min_silence_duration=0.45)
    if _STT is None:
        _STT = LocalSTT(CFG["stt"]["kind"], CFG["stt"]["model"],
                        revision=CFG["stt"].get("revision"),
                        allow_unfrozen=CFG["stt"].get("allow_unfrozen", False))
        _STT.load()
        if CFG["stt"]["kind"] == "parakeet-mlx":
            _STT.warmup()  # measured on MLX; keep startup cost outside the first user turn
    if _TTS is None:
        _TTS = KokoroTTS(CFG["tts"].get("voice", "af_heart"), CFG["tts"].get("speed", 1.0))
        _TTS.load()
        _TTS.render("Ready.")  # first ONNX run is slow; do it before any room
    log.info("prewarm done: profile=%s llm=%s", CFG["profile"], CFG["llm"]["model"])


server = AgentServer(job_executor_type=JobExecutorType.THREAD, setup_fnc=prewarm, num_idle_processes=1)


def build_manifest(room_name: str):
    mode = room_mode(room_name)
    if mode == "car":
        from .tools.car_tools import build_car_manifest
        return mode, build_car_manifest()
    from .tools.bench_tools import build_bench_manifest
    return mode, build_bench_manifest()


class DuplexAgent(Agent):
    """Hands every finished user turn to the coordinator instead of an LLM node."""

    def __init__(self, coordinator: Coordinator) -> None:
        super().__init__(instructions="(turns are handled by the PRISM coordinator)")
        self.coordinator = coordinator

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        text = new_message.text_content or ""
        self.coordinator.on_user_turn(text)
        raise llm.StopResponse()


@server.rtc_session()
async def entrypoint(ctx: agents.JobContext) -> None:
    prewarm()
    room = ctx.room.name
    mode, manifest = build_manifest(room)
    log.info("joining room %s (mode=%s)", room, mode)
    with open(fdb_logger.HEARTBEAT_LOG, "a") as f:
        f.write(f"!!! PRISM AGENT JOINING ROOM: {room} at {time.ctime()} !!!\n")

    # ── per-conversation state: nothing is shared between rooms ──
    bus, ledger = EventBus(), Ledger()
    llm_client = make_llm(CFG)
    executor = Executor(manifest, ledger, bus,
                        attempt_logger=fdb_logger.FDBToolLogger(room))

    stt = LocalSTT(CFG["stt"]["kind"], CFG["stt"]["model"],
                        revision=CFG["stt"].get("revision"),
                        allow_unfrozen=CFG["stt"].get("allow_unfrozen", False))   # per room; model shared
    session = AgentSession(
        vad=_VAD, stt=stt, tts=_TTS, llm=None,
        turn_handling=local_turn_handling(),
    )
    speech_output: RecognitionAwareAudioOutput | None = None

    async def speak(text: str) -> None:
        handle = session.say(text, add_to_chat_ctx=False)
        bus.emit("speech_handle", speech_id=handle.id, state="queued")
        try:
            await handle
        except asyncio.CancelledError:
            handle.interrupt()
            raise
        finally:
            failure = handle.exception() if handle.done() else None
            bus.emit("speech_handle", speech_id=handle.id,
                     state="failed" if failure else "interruption_requested" if handle.interrupted else "finished",
                     error=str(failure) if failure else None)

    coord = Coordinator(IntentResolver(llm_client, manifest), executor, ledger, bus, speak,
                        Responder(llm_client), make_coordinator_config(CFG, mode=mode),
                        retranscribe=stt.transcribe_since)
    # ── event sinks: JSONL trace + data channel for the web UI ──
    trace = TraceWriter(TRACE_DIR / f"{room}.jsonl")

    ui_tasks: set[asyncio.Task] = set()
    closing = False

    def to_ui(ev: E.Event) -> None:
        if closing:
            return
        payload = json.dumps({**ev.to_dict(), "snapshot": coord.snapshot()}, default=str)
        task = asyncio.create_task(ctx.room.local_participant.publish_data(payload, reliable=True, topic="agent-events"))
        ui_tasks.add(task)
        def finished(t):
            ui_tasks.discard(t)
            if not t.cancelled():
                t.exception()
        task.add_done_callback(finished)

    bus.subscribe(trace)
    bus.subscribe(to_ui)

    # These state events are server proxies, not listener-side acoustic timings.

    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        bus.emit("user_state", state=ev.new_state)
        if ev.new_state == "speaking":
            coord.on_user_speaking()
        elif ev.new_state == "listening":
            coord.on_user_listening()
        if speech_output is not None:
            speech_output.user_state_changed()

    @session.on("user_input_transcribed")
    def _on_transcript(ev) -> None:
        if not ev.is_final:
            coord.on_user_partial(ev.transcript)

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        bus.emit("agent_state", state=ev.new_state)

    @session.on("agent_false_interruption")
    def _on_false_interruption(ev) -> None:
        # This is the SDK's resume decision; the output guard may still hold PCM.
        bus.emit("agent_false_interruption", resumed=ev.resumed)

    @session.on("close")
    def _on_close(ev) -> None:
        nonlocal closing
        closing = True
        if speech_output is not None:
            speech_output.close()
        # Session closure alone leaves the room job alive. Job shutdown owns
        # coordinator, HTTP client and trace cleanup through the callback below.
        ctx.shutdown(reason=f"agent session closed: {ev.reason.value}")

    async def _cleanup() -> None:
        nonlocal closing
        closing = True
        if speech_output is not None:
            speech_output.close()
            stt.off("recognition_completed", speech_output.recognition_completed)
        log.info("room cleanup started: %s", room)
        try:
            await coord.aclose(timeout=5)
        finally:
            for task in ui_tasks:
                task.cancel()
            try:
                await asyncio.gather(*ui_tasks, return_exceptions=True)
                await llm_client.client.close()
            finally:
                trace_complete = await trace.aclose(timeout=5)
        if trace_complete:
            log.info("room cleanup finished: %s", room)
        else:
            log.error("room cleanup incomplete: %s (trace)", room)

    ctx.add_shutdown_callback(_cleanup)
    await trace.start()
    await session.start(room=ctx.room, agent=DuplexAgent(coord))
    if not closing:
        if (audio_output := session.output.audio) is not None and audio_output.can_pause:
            speech_output = RecognitionAwareAudioOutput(
                audio_output, current_speech=lambda: session.current_speech,
                pending_recognitions=lambda: stt.pending_recognitions,
                user_speaking=lambda: session.user_state == "speaking", emit=bus.emit)
            stt.on("recognition_completed", speech_output.recognition_completed)
            session.output.audio = speech_output
        bus.emit("session_started", room=room, mode=mode, profile=CFG["profile"], llm=CFG["llm"]["model"])


if __name__ == "__main__":
    agents.cli.run_app(server)
