"""PRISM Duplex agent: LiveKit entrypoint.

    python -m agent.main start     # worker; auto-joins every new room
    python -m agent.main dev       # same, with reload

Mode per room: rooms named "car-*" use the in-car navigation manifest
(extension); every other room uses the FDB-v3 benchmark tools. MODE=car|bench
forces one.

Audio pipeline (all local): Silero VAD -> Parakeet STT -> our Coordinator
(resolver LLM + commit gate + executor + ledger) -> Kokoro TTS. There is no
LiveKit LLM node: every user turn is handed to the coordinator, which decides
what to say and which tools to run.
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

from .config import ROOT, load_config, make_coordinator_config, make_llm
from .coordinator import events as E
from .coordinator.coordinator import Coordinator
from .coordinator.events import EventBus
from .coordinator.executor import Executor
from .coordinator.ledger import Ledger
from .coordinator.resolver import IntentResolver
from .coordinator.responder import Responder
from .pipeline.local_stt import LocalSTT
from .pipeline.local_tts import KokoroTTS
from .telemetry import fdb_logger

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
        _STT = LocalSTT(CFG["stt"]["kind"], CFG["stt"]["model"])
        _STT.load()
    if _TTS is None:
        _TTS = KokoroTTS(CFG["tts"].get("voice", "af_heart"), CFG["tts"].get("speed", 1.0))
        _TTS.load()
        _TTS.render("Ready.")  # first ONNX run is slow; do it before any room
    log.info("prewarm done: profile=%s llm=%s", CFG["profile"], CFG["llm"]["model"])


server = AgentServer(job_executor_type=JobExecutorType.THREAD, setup_fnc=prewarm, num_idle_processes=1)


def build_manifest(room_name: str):
    mode = os.getenv("MODE") or ("car" if room_name.startswith("car-") else "bench")
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
                        lambda fn, args, t0, t1: fdb_logger.log_tool_call(room, fn, args, t0, t1))

    stt = LocalSTT(CFG["stt"]["kind"], CFG["stt"]["model"])   # per room; model shared
    session = AgentSession(
        vad=_VAD, stt=stt, tts=_TTS, llm=None,
        min_endpointing_delay=float(os.getenv("PRISM_MIN_ENDPOINTING", "0.4")),
        max_endpointing_delay=3.0,
        allow_interruptions=True,
    )

    async def speak(text: str) -> None:
        handle = session.say(text, add_to_chat_ctx=False)
        await handle

    coord = Coordinator(IntentResolver(llm_client, manifest), executor, ledger, bus, speak,
                        Responder(llm_client), make_coordinator_config(CFG),
                        retranscribe=stt.transcribe_since)
    if mode == "bench":
        # the benchmark records only a short window after the user stops; keep speech tight
        coord.cfg.speak_results = os.getenv("PRISM_BENCH_SPEAK_RESULTS", "0") == "1"

    # ── event sinks: JSONL trace + data channel for the web UI ──
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    trace_f = open(TRACE_DIR / f"{room}.jsonl", "a")

    def to_trace(ev: E.Event) -> None:
        trace_f.write(json.dumps(ev.to_dict(), default=str) + "\n")
        trace_f.flush()

    def to_ui(ev: E.Event) -> None:
        payload = json.dumps({**ev.to_dict(), "snapshot": coord.snapshot()}, default=str)
        asyncio.ensure_future(ctx.room.local_participant.publish_data(payload, reliable=True, topic="agent-events"))

    bus.subscribe(to_trace)
    bus.subscribe(to_ui)

    # ── latency bookkeeping in the reference format (optional benchmark metric) ──
    lat = {"logged": False}

    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        bus.emit("user_state", state=ev.new_state)
        if ev.new_state == "speaking":
            coord.on_user_speaking()
        elif ev.new_state == "listening":
            coord.on_user_listening()

    @session.on("user_input_transcribed")
    def _on_transcript(ev) -> None:
        if not ev.is_final:
            coord.on_user_partial(ev.transcript)

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        bus.emit("agent_state", state=ev.new_state)
        if ev.new_state == "speaking" and not lat["logged"] and coord.user_done_at:
            done = [e for e in bus.of_type(E.TOOL_DONE)]
            if done:
                lat["logged"] = True
                t0 = done[0].ts - done[0].data.get("duration_ms", 0) / 1000
                fdb_logger.log_latency(room, done[0].data["tool"], coord.user_done_at, t0, done[-1].ts, time.time())

    async def _cleanup() -> None:
        await coord.drain(timeout=5)
        trace_f.close()

    ctx.add_shutdown_callback(_cleanup)
    await session.start(room=ctx.room, agent=DuplexAgent(coord))
    bus.emit("session_started", room=room, mode=mode, profile=CFG["profile"], llm=CFG["llm"]["model"])


if __name__ == "__main__":
    agents.cli.run_app(server)
