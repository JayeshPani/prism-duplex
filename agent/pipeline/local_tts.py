"""Local text-to-speech (Kokoro-82M via ONNX) as a LiveKit TTS plugin."""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

MODELS_DIR = Path(__file__).resolve().parents[2] / "models" / "kokoro"
_SR = 24000


class KokoroTTS(tts.TTS):
    def __init__(self, voice: str = "af_heart", speed: float = 1.0) -> None:
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=_SR, num_channels=1)
        self.voice = voice
        self.speed = speed
        self._engine = None
        self._load_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")  # shared by all rooms

    @property
    def model(self) -> str:
        return "kokoro-v1.0"

    @property
    def provider(self) -> str:
        return "kokoro-onnx"

    def load(self):
        with self._load_lock:
            if self._engine is None:
                from kokoro_onnx import Kokoro
                self._engine = Kokoro(str(MODELS_DIR / "kokoro-v1.0.onnx"), str(MODELS_DIR / "voices-v1.0.bin"))
        return self._engine

    def render(self, text: str) -> bytes:
        samples, sr = self.load().create(text, voice=self.voice, speed=self.speed, lang="en-us")
        assert sr == _SR
        return (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()

    def synthesize(self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> tts.ChunkedStream:
        return _KokoroStream(tts=self, input_text=text, conn_options=conn_options)


class _KokoroStream(tts.ChunkedStream):
    def __init__(self, *, tts: KokoroTTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._k = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(request_id=uuid.uuid4().hex, sample_rate=_SR, num_channels=1,
                                  mime_type="audio/pcm")
        pcm = await asyncio.get_running_loop().run_in_executor(self._k._pool, self._k.render, self.input_text)
        output_emitter.push(pcm)
        output_emitter.flush()
