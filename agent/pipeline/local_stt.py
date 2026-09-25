"""Local speech-to-text as a LiveKit STT plugin.

Non-streaming: LiveKit's AgentSession wraps it in a VAD-driven StreamAdapter,
so each detected speech segment is transcribed as a whole. Parakeet-TDT
transcribes a few seconds of speech in well under 200 ms on an M4 / any GPU.

Pauses split one sentence into several VAD segments, and a fragment like
"add to item" loses words that the whole sentence would keep. So each
conversation's STT keeps the audio of its recent segments, and the
coordinator can ask for the whole not-yet-committed span to be re-transcribed
in one pass (`transcribe_since`).

Backends:
  parakeet-mlx   Apple Silicon (MLX)
  nemo-parakeet  NVIDIA GPU / CPU via NeMo (same checkpoint the benchmark uses for ASR)
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
from livekit import rtc
from livekit.agents import stt, utils
from livekit.agents.types import NOT_GIVEN, APIConnectOptions, NotGivenOr

_SR = 16000
_GAP = np.zeros(int(0.25 * _SR), dtype=np.int16)   # silence placed between re-joined segments
log = logging.getLogger("prism.stt")


class _Engine:
    """One loaded model per process, shared by every conversation. MLX binds its
    compute streams to the creating thread, so load and inference share one thread
    (which also serializes inference across rooms)."""

    _instances: dict[tuple[str, str], "_Engine"] = {}
    _guard = threading.Lock()

    def __init__(self, kind: str, model: str) -> None:
        self.kind, self.model_name = kind, model
        self.model: Any = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")

    @classmethod
    def get(cls, kind: str, model: str) -> "_Engine":
        with cls._guard:
            key = (kind, model)
            if key not in cls._instances:
                cls._instances[key] = _Engine(kind, model)
            return cls._instances[key]

    def _load(self) -> None:
        if self.model is not None:
            return
        if self.kind == "parakeet-mlx":
            from parakeet_mlx import from_pretrained
            self.model = from_pretrained(self.model_name)
        elif self.kind == "nemo-parakeet":
            import nemo.collections.asr as nemo_asr
            import torch
            m = nemo_asr.models.ASRModel.from_pretrained(model_name=self.model_name)
            self.model = m.cuda() if torch.cuda.is_available() else m
            self.model.eval()
        else:
            raise ValueError(f"unknown STT kind {self.kind}")

    def load(self) -> None:
        self.pool.submit(self._load).result()

    def _transcribe(self, pcm16: np.ndarray) -> str:
        self._load()
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(_SR)
                w.writeframes(pcm16.tobytes())
            if self.kind == "parakeet-mlx":
                return self.model.transcribe(path).text.strip()
            r = self.model.transcribe([path], verbose=False)[0]
            return (r.text if hasattr(r, "text") else str(r)).strip()
        finally:
            os.unlink(path)

    def transcribe_blocking(self, pcm16: np.ndarray) -> str:
        return self.pool.submit(self._transcribe, pcm16).result()

    async def transcribe(self, pcm16: np.ndarray) -> str:
        return await asyncio.get_running_loop().run_in_executor(self.pool, self._transcribe, pcm16)


class LocalSTT(stt.STT):
    """Per-conversation STT: shares the engine, keeps its own recent segments."""

    def __init__(self, kind: str, model: str, keep_s: float = 45.0) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._engine = _Engine.get(kind, model)
        self._keep_s = keep_s
        self._segments: deque[tuple[float, np.ndarray]] = deque()   # (end wall time, pcm16)

    @property
    def model(self) -> str:
        return self._engine.model_name

    @property
    def provider(self) -> str:
        return self._engine.kind

    def load(self) -> None:
        self._engine.load()

    def transcribe(self, pcm16: np.ndarray) -> str:
        return self._engine.transcribe_blocking(pcm16)

    def segments_since(self, t: float) -> int:
        return sum(1 for end, _ in self._segments if end > t)

    async def transcribe_since(self, t: float) -> str | None:
        """Re-transcribe every segment that ended after `t` as one utterance."""
        segs = [pcm for end, pcm in self._segments if end > t]
        if len(segs) < 2:
            return None
        joined = np.concatenate([x for s in segs for x in (s, _GAP)][:-1])
        t0 = time.perf_counter()
        text = await self._engine.transcribe(joined)
        log.info("re-stt %d segments (%.2fs) in %.3fs: %s", len(segs), len(joined) / _SR,
                 time.perf_counter() - t0, text[:80])
        return text

    async def _recognize_impl(self, buffer: utils.AudioBuffer, *,
                              language: NotGivenOr[str] = NOT_GIVEN,
                              conn_options: APIConnectOptions) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer)
        if frame.sample_rate != _SR or frame.num_channels != 1:
            resampler = rtc.AudioResampler(frame.sample_rate, _SR, num_channels=frame.num_channels)
            frames = resampler.push(frame) + resampler.flush()
            frame = rtc.combine_audio_frames(frames)
        pcm = np.frombuffer(bytes(frame.data), dtype=np.int16)
        if frame.num_channels > 1:
            pcm = pcm.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
        now = time.time()
        self._segments.append((now, pcm))
        while self._segments and now - self._segments[0][0] > self._keep_s:
            self._segments.popleft()
        t0 = time.perf_counter()
        text = await self._engine.transcribe(pcm)
        log.info("stt %.2fs audio in %.3fs: %s", len(pcm) / _SR, time.perf_counter() - t0, text[:60])
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(text=text, language="en")],
        )
