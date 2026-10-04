"""Diagnostic fidelity tests: real _Engine/temp-WAV path, fake speech model only."""
import asyncio
from contextvars import ContextVar
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from agent.pipeline import local_stt

SPEC = importlib.util.spec_from_file_location("iteration12_stt_diagnostics", Path(__file__).with_name("stt_diagnostics.py"))
DIAGNOSTICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTICS)
ROOM = ContextVar("test_room", default="unassigned")
AMBIENT = ContextVar("unrelated_context", default="worker-default")


class Gate:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.guard_released = threading.Event()

    def hold(self):
        def guard():
            self.guard_released.set()
            self.release.set()
        timer = threading.Timer(2, guard)
        timer.daemon = True
        timer.start()
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            self.release.wait()
        finally:
            timer.cancel()


@pytest.fixture
def installed(monkeypatch):
    fake = SimpleNamespace()
    observed = {"pcm": [], "paths": [], "ambient": [], "generate_calls": 0}

    def load_audio(path):
        observed["paths"].append(path)
        with wave.open(str(path), "rb") as stream:
            assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (16000, 1, 2)
            pcm = stream.readframes(stream.getnframes())
        observed["pcm"].append(pcm)
        return pcm

    def get_logmel(audio):
        return audio

    class BaseParakeet:
        def transcribe(self, path):
            return self.generate(fake.get_logmel(fake.load_audio(path)))[0]

    class ParakeetTDT(BaseParakeet):
        gate = None
        error = None

        def generate(self, mel):
            observed["generate_calls"] += 1
            observed["ambient"].append(AMBIENT.get())
            if self.gate is not None:
                self.gate.hold()
            if self.error is not None:
                raise self.error
            return [SimpleNamespace(text="  recognized  ")]

    fake.load_audio, fake.get_logmel = load_audio, get_logmel
    fake.BaseParakeet, fake.ParakeetTDT = BaseParakeet, ParakeetTDT
    engine = local_stt._Engine("parakeet-mlx", "fake-test-model", "test-revision")
    engine.model = model = ParakeetTDT()
    monkeypatch.setattr(local_stt, "load_stt_model", lambda *args, **kwargs: model)
    monkeypatch.setattr(local_stt._Engine, "get", classmethod(lambda cls, *args, **kwargs: engine))
    original_submit = DIAGNOSTICS.ThreadPoolExecutor.submit
    original_native = local_stt._Engine._transcribe
    observer = DIAGNOSTICS.STTDiagnostics(lambda: SimpleNamespace(room=SimpleNamespace(name=ROOM.get())))
    observer.install(local_stt, fake)
    stt = local_stt.LocalSTT("parakeet-mlx", "fake-test-model", revision="test-revision")
    value = SimpleNamespace(observer=observer, engine=engine, model=model, observed=observed, stt=stt, fake=fake)
    try:
        yield value
    finally:
        if model.gate is not None:
            model.gate.release.set()
        engine.pool.shutdown(wait=True)
        observer.restore()
        assert DIAGNOSTICS.ThreadPoolExecutor.submit is original_submit
        assert local_stt._Engine._transcribe is original_native


async def drain(engine):
    await asyncio.wait_for(asyncio.wrap_future(engine.pool.submit(lambda: None)), 3)


def flush(installed, directory):
    installed.observer.restore()
    assert installed.observer.flush(directory), installed.observer.flush_error
    return json.loads((directory / "report.json").read_text())


@pytest.mark.asyncio
async def test_original_pipeline_stages_pcm_room_and_context_isolation(installed, tmp_path):
    x = installed
    first, second = np.arange(10, dtype=np.int16), np.arange(20, 30, dtype=np.int16)
    x.stt._segments.extend([(1.0, first), (2.0, second)])
    expected_pcm = np.concatenate([first, local_stt._GAP, second]).tobytes()
    room_token, ambient_token = ROOM.set("room-a"), AMBIENT.set("caller-private")
    try:
        assert await x.stt.transcribe_since(0) == "recognized"
    finally:
        ROOM.reset(room_token)
        AMBIENT.reset(ambient_token)
    assert x.observed["ambient"] == ["worker-default"]
    assert x.observed["pcm"] == [expected_pcm]
    assert all(not Path(p).exists() for p in x.observed["paths"])
    assert await asyncio.wrap_future(x.engine.pool.submit(lambda: x.observer._call.get())) is None
    assert len(x.observer.calls) == 1
    assert not list(tmp_path.iterdir()), "diagnostics wrote before explicit post-run flush"
    report = flush(x, tmp_path / "diagnostics")
    row = report["calls"][0]
    assert row["room"] == "room-a" and row["call_type"] == "transcribe_since"
    assert row["request_id"] == report["requests"][0]["request_id"]
    ordered = [row[key]["monotonic_ns"] for key in ("engine_api_entry", "submitted", "worker_entry", "native_completion", "caller_return")]
    assert ordered == sorted(ordered)
    assert row["native_completion"]["result_text"] == row["caller_return"]["result_text"] == "recognized"
    stages = {s["name"] for s in row["stages"]}
    assert stages == {"engine_load_check", "tempfile_create", "wave_open", "wave_writeframes", "wave_close",
                      "model_transcribe", "parakeet_load_audio", "parakeet_get_logmel", "parakeet_generate", "tempfile_unlink"}
    assert row["submitted_equals_written_pcm"] is True
    for label in ("submitted_pcm", "written_wav_pcm"):
        assert (tmp_path / "diagnostics" / row[label]["file"]).read_bytes() == expected_pcm
        assert row[label]["sha256"] == sha256(expected_pcm).hexdigest()
    assert report["observation_error_count"] == 0 and report["pending_native_call_ids"] == []


@pytest.mark.asyncio
async def test_keyword_calls_preserve_async_and_blocking_contract(installed):
    x = installed
    pcm = np.array([7, -7], dtype=np.int16)
    assert await x.engine.transcribe(pcm16=pcm) == "recognized"
    assert x.engine.transcribe_blocking(pcm16=pcm) == "recognized"
    assert x.observed["pcm"] == [pcm.tobytes(), pcm.tobytes()]
    assert [r["engine_api"] for r in x.observer.calls] == ["transcribe", "transcribe_blocking"]


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_claim_native_completion(installed, tmp_path):
    x = installed
    x.model.gate = gate = Gate()
    task = asyncio.create_task(x.engine.transcribe(np.array([1, 2], dtype=np.int16)))
    try:
        await asyncio.wait_for(gate.entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = x.observer.calls[0]
        assert row["caller_return"]["status"] == "cancelled"
        assert "native_completion" not in row and "native_future_done" not in row
        # Snapshot without a native wait preserves the pending call, not a false completion.
        assert x.observer.flush(tmp_path / "pending")
        pending = json.loads((tmp_path / "pending/report.json").read_text())
        assert pending["pending_native_call_ids"] == [row["call_id"]]
        gate.release.set()
        await drain(x.engine)
        assert not gate.guard_released.is_set()
        report = flush(x, tmp_path / "completed")
        final = report["calls"][0]
        assert final["caller_return"]["status"] == "cancelled"
        assert final["native_completion"]["status"] == "returned"
        assert final["native_future_done"]["cancelled"] is False
        assert final["native_completion"]["monotonic_ns"] > final["caller_return"]["monotonic_ns"]
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_queued_call_never_enters_native_worker(installed, tmp_path):
    x = installed
    x.model.gate = gate = Gate()
    first = asyncio.create_task(x.engine.transcribe(np.array([1], dtype=np.int16)))
    second = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 3)
        second = asyncio.create_task(x.engine.transcribe(np.array([2], dtype=np.int16)))
        await asyncio.sleep(0)
        assert len(x.observer.calls) == 2 and "submitted" in x.observer.calls[1]
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        await asyncio.sleep(0)
        gate.release.set()
        assert await first == "recognized"
        await drain(x.engine)
        report = flush(x, tmp_path / "diagnostics")
        cancelled = report["calls"][1]
        assert cancelled["native_future_done"]["status"] == "cancelled_before_entry"
        assert "worker_entry" not in cancelled and "native_completion" not in cancelled
        assert "written_wav_pcm" not in cancelled and "submitted_pcm" in cancelled
        assert x.observed["generate_calls"] == 1
    finally:
        gate.release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_original_exception_and_temp_cleanup_are_preserved(installed, tmp_path):
    x = installed
    error = x.model.error = RuntimeError("fake recognizer failed")
    with pytest.raises(RuntimeError) as caught:
        await x.engine.transcribe(np.array([3], dtype=np.int16))
    assert caught.value is error
    assert all(not Path(p).exists() for p in x.observed["paths"])
    await drain(x.engine)
    report = flush(x, tmp_path / "diagnostics")
    row = report["calls"][0]
    assert row["caller_return"]["error"] == row["native_completion"]["error"] == repr(error)
    assert next(s for s in row["stages"] if s["name"] == "parakeet_generate")["status"] == "error"
    assert next(s for s in row["stages"] if s["name"] == "tempfile_unlink")["status"] == "returned"
    assert x.observer.flush(tmp_path / "diagnostics") is False
    assert "FileExistsError" in x.observer.flush_error


@pytest.mark.asyncio
async def test_diagnostic_limits_do_not_change_original_transcription(installed, tmp_path):
    x = installed
    x.observer.max_pcm_bytes = 1
    x.observer.max_calls = 1
    pcm = np.array([4, 5], dtype=np.int16)
    assert await x.engine.transcribe(pcm) == await x.engine.transcribe(pcm) == "recognized"
    report = flush(x, tmp_path / "diagnostics")
    assert report["dropped_calls"] == 1 and report["retained_pcm_bytes"] == 0
    assert report["calls"][0]["submitted_pcm"]["status"] == "retention_limit_exceeded"
    assert report["calls"][0]["written_wav_pcm"]["status"] == "retention_limit_exceeded"
    assert not list((tmp_path / "diagnostics/pcm").iterdir())
    assert x.observed["generate_calls"] == 2
