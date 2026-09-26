"""Experiment-only observation of the original local STT and Parakeet methods.

Install before worker launch; restore and flush to a NEW directory after launcher
exit. No model import/loading, evaluation barrier, argument substitution, native
wait or diagnostic file write occurs in a recognition hook.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
import inspect
import json
from pathlib import Path
from threading import RLock, get_ident
import time
from unittest.mock import patch


def stamp():
    return {"monotonic_ns": time.monotonic_ns(), "unix_ns": time.time_ns(), "thread_id": get_ident()}


def outcome(error=None, result=None):
    if error is not None:
        return {"status": "cancelled" if type(error).__name__ == "CancelledError" else "error",
                "error_type": type(error).__name__, "error": repr(error)}
    return {"status": "returned", "result_text": result if isinstance(result, str) else None}


class ModuleView:
    """Override only this importing module's selected function references."""
    def __init__(self, original, **overrides):
        self.original, self.overrides = original, overrides

    def __getattr__(self, name):
        return self.overrides.get(name, getattr(self.original, name))


class STTDiagnostics:
    def __init__(self, get_context, *, max_calls=128, max_pcm_bytes=32 * 1024 * 1024):
        self.get_context = get_context
        self.max_calls, self.max_pcm_bytes = max_calls, max_pcm_bytes
        self.calls, self.requests, self.errors = [], [], []
        self.retained_bytes = self.dropped_calls = self.dropped_requests = self.error_count = 0
        self.observation_total_ns = self.observation_max_ns = self.observation_hooks = 0
        self.flush_error = None
        self._pcm, self._waves = {}, {}
        self._call = ContextVar("stt_diagnostic_call", default=None)
        self._origin = ContextVar("stt_diagnostic_origin", default=None)
        self._lock, self._patches = RLock(), None
        self.source_sha256 = {}

    def _observe(self, fn, *args, **kwargs):
        began = time.perf_counter_ns()
        with self._lock:
            try:
                return fn(*args, **kwargs)
            except Exception as error:
                self.error_count += 1
                if len(self.errors) < 20:
                    self.errors.append(repr(error))
            finally:
                elapsed = time.perf_counter_ns() - began
                self.observation_hooks += 1
                self.observation_total_ns += elapsed
                self.observation_max_ns = max(self.observation_max_ns, elapsed)

    def _new_request(self, kind):
        if len(self.requests) >= self.max_calls:
            self.dropped_requests += 1
            return None
        row = {"request_id": f"stt-request-{len(self.requests) + 1:03d}", "call_type": kind,
               "room": None, "identity_error": None, "entry": stamp(), "stages": []}
        try:
            row["room"] = self.get_context().room.name
        except Exception as error:
            row["identity_error"] = repr(error)
        self.requests.append(row)
        return row

    def _capture(self, row, label, data, *, array=False):
        requested = int(data.nbytes) if array else len(data)
        record = {"requested_bytes": requested, "sample_rate": 16000, "sample_format": "native int16 mono"}
        if self.retained_bytes + requested > self.max_pcm_bytes:
            record["status"] = "retention_limit_exceeded"
        else:
            payload = data.tobytes() if array else bytes(data)
            self._pcm[(row["call_id"], label)] = payload
            self.retained_bytes += len(payload)
            record.update(status="retained", bytes=len(payload))
        row[label] = record

    def _new_call(self, engine, pcm, kind):
        if len(self.calls) >= self.max_calls:
            self.dropped_calls += 1
            return None
        origin = self._origin.get() or {}
        row = {"call_id": f"stt-call-{len(self.calls) + 1:03d}", "request_id": origin.get("request_id"),
               "room": origin.get("room"), "call_type": origin.get("call_type", kind), "engine_api": kind,
               "engine_identity": id(engine), "model_kind": engine.kind, "model_name": engine.model_name,
               "revision": engine.revision, "engine_api_entry": stamp(), "stages": []}
        self.calls.append(row)
        self._capture(row, "submitted_pcm", pcm, array=True)
        return row

    def _mark(self, row, key, **values):
        if row is not None:
            row[key] = {**stamp(), **values}

    def _begin_stage(self, name, args):
        active = self._call.get()
        row = active[0] if active else self._origin.get()
        if row is None:
            return None
        if len(row["stages"]) >= 64:
            row["dropped_stages"] = row.get("dropped_stages", 0) + 1
            return None
        stage = {"name": name, "start": stamp()}
        if name == "engine_load_check":
            stage["model_present_before"] = args[0].model is not None
        row["stages"].append(stage)
        return stage

    def _finish_stage(self, stage, error):
        if stage is not None:
            stage.update(end=stamp(), **outcome(error))

    def _stage(self, name, original):
        @wraps(original)
        def measured(*args, **kwargs):
            stage = self._observe(self._begin_stage, name, args)
            error = None
            try:
                return original(*args, **kwargs)
            except BaseException as caught:
                error = caught
                raise
            finally:
                self._observe(self._finish_stage, stage, error)
        return measured

    def _request_wrapper(self, kind, original):
        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def async_request(*args, **kwargs):
                row = self._observe(self._new_request, kind)
                token = self._origin.set(row)
                result = error = None
                try:
                    result = await original(*args, **kwargs)
                    return result
                except BaseException as caught:
                    error = caught
                    raise
                finally:
                    self._observe(self._mark, row, "return", **outcome(error, result))
                    self._origin.reset(token)
            return async_request

        @wraps(original)
        def sync_request(*args, **kwargs):
            row = self._observe(self._new_request, kind)
            token = self._origin.set(row)
            result = error = None
            try:
                result = original(*args, **kwargs)
                return result
            except BaseException as caught:
                error = caught
                raise
            finally:
                self._observe(self._mark, row, "return", **outcome(error, result))
                self._origin.reset(token)
        return sync_request

    def _engine_wrapper(self, kind, original):
        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def asynchronous(engine, pcm16):
                row = self._observe(self._new_call, engine, pcm16, kind)
                token = self._call.set((row, engine) if row is not None else None)
                result = error = None
                try:
                    result = await original(engine, pcm16)
                    return result
                except BaseException as caught:
                    error = caught
                    raise
                finally:
                    self._observe(self._mark, row, "caller_return", **outcome(error, result))
                    self._call.reset(token)
            return asynchronous

        @wraps(original)
        def blocking(engine, pcm16):
            row = self._observe(self._new_call, engine, pcm16, kind)
            token = self._call.set((row, engine) if row is not None else None)
            result = error = None
            try:
                result = original(engine, pcm16)
                return result
            except BaseException as caught:
                error = caught
                raise
            finally:
                self._observe(self._mark, row, "caller_return", **outcome(error, result))
                self._call.reset(token)
        return blocking

    def install(self, local_stt, parakeet):
        if self._patches is not None:
            raise RuntimeError("diagnostics already installed")
        stack = self._patches = ExitStack()
        engine_cls = local_stt._Engine
        originals = {name: getattr(engine_cls, name) for name in ("_transcribe", "_load", "transcribe", "transcribe_blocking")}
        submit_original = ThreadPoolExecutor.submit
        wave_module, tempfile_module, os_module = local_stt.wave, local_stt.tempfile, local_stt.os
        source_files = {Path(__file__), Path(local_stt.__file__)}
        for item in (parakeet.load_audio, parakeet.get_logmel, parakeet.BaseParakeet.transcribe, parakeet.ParakeetTDT.generate):
            path = inspect.getsourcefile(item)
            if path:
                source_files.add(Path(path))
        self.source_sha256 = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in source_files}

        def replace(obj, name, value):
            stack.enter_context(patch.object(obj, name, value))

        def future_done(row, future):
            def record():
                error = None if future.cancelled() else future.exception()
                self._mark(row, "native_future_done", cancelled=future.cancelled(),
                           **({"status": "cancelled_before_entry"} if future.cancelled() else outcome(error)))
            self._observe(record)

        @wraps(submit_original)
        def submit(pool, function, *args, **kwargs):
            active = self._call.get()
            if not active or pool is not active[1].pool or getattr(function, "__self__", None) is not active[1] or getattr(function, "__name__", None) != "_transcribe":
                return submit_original(pool, function, *args, **kwargs)
            row, _ = active
            origin = self._origin.get()
            self._observe(self._mark, row, "submitted")

            def dispatch():
                # Propagate diagnostic identity only, not previously absent SDK context.
                token, origin_token = self._call.set(active), self._origin.set(origin)
                try:
                    return function(*args, **kwargs)
                finally:
                    self._origin.reset(origin_token)
                    self._call.reset(token)

            future = submit_original(pool, dispatch)
            future.add_done_callback(lambda done: future_done(row, done))
            return future

        @wraps(originals["_transcribe"])
        def native(engine, pcm16):
            active = self._call.get()
            row = active[0] if active else None
            self._observe(self._mark, row, "worker_entry")
            result = error = None
            try:
                result = originals["_transcribe"](engine, pcm16)
                return result
            except BaseException as caught:
                error = caught
                raise
            finally:
                self._observe(self._mark, row, "native_completion", **outcome(error, result))

        def wave_open(*args, **kwargs):
            result = self._stage("wave_open", wave_module.open)(*args, **kwargs)
            active = self._call.get()
            if active and isinstance(result, wave_module.Wave_write):
                self._observe(self._waves.__setitem__, id(result), active[0])
            return result

        original_write, original_close = wave_module.Wave_write.writeframes, wave_module.Wave_write.close

        def writeframes(wave_writer, data):
            row = self._observe(self._waves.get, id(wave_writer))
            if row is None:
                return original_write(wave_writer, data)
            self._observe(self._capture, row, "written_wav_pcm", data)
            return self._stage("wave_writeframes", original_write)(wave_writer, data)

        def wave_close(wave_writer):
            row = self._observe(self._waves.pop, id(wave_writer), None)
            if row is None:
                return original_close(wave_writer)
            return self._stage("wave_close", original_close)(wave_writer)

        replace(ThreadPoolExecutor, "submit", submit)
        replace(engine_cls, "_transcribe", native)
        replace(engine_cls, "_load", self._stage("engine_load_check", originals["_load"]))
        for name in ("transcribe", "transcribe_blocking"):
            replace(engine_cls, name, self._engine_wrapper(name, originals[name]))
        for name in ("load", "warmup", "transcribe", "transcribe_since", "_recognize_impl"):
            replace(local_stt.LocalSTT, name, self._request_wrapper(name, getattr(local_stt.LocalSTT, name)))
        replace(local_stt, "tempfile", ModuleView(tempfile_module, mkstemp=self._stage("tempfile_create", tempfile_module.mkstemp)))
        replace(local_stt, "wave", ModuleView(wave_module, open=wave_open))
        replace(local_stt, "os", ModuleView(os_module, unlink=self._stage("tempfile_unlink", os_module.unlink)))
        replace(wave_module.Wave_write, "writeframes", writeframes)
        replace(wave_module.Wave_write, "close", wave_close)
        for name in ("load_audio", "get_logmel"):
            replace(parakeet, name, self._stage("parakeet_" + name, getattr(parakeet, name)))
        replace(parakeet.BaseParakeet, "transcribe", self._stage("model_transcribe", parakeet.BaseParakeet.transcribe))
        replace(parakeet.ParakeetTDT, "generate", self._stage("parakeet_generate", parakeet.ParakeetTDT.generate))

    def restore(self):
        if self._patches is not None:
            self._patches.close()
            self._patches = None

    def flush(self, output):
        """After launcher exit only. Return False on failure; never mask its exception."""
        try:
            output = Path(output)
            with self._lock:
                # Freeze a bounded snapshot; late native completions stay explicitly pending.
                calls = json.loads(json.dumps(self.calls))
                requests = json.loads(json.dumps(self.requests))
                pcm = dict(self._pcm)
                report = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "calls": calls, "requests": requests,
                          "source_sha256_before": self.source_sha256, "dropped_calls": self.dropped_calls,
                          "dropped_requests": self.dropped_requests, "retained_pcm_bytes": self.retained_bytes,
                          "limits_config": {"max_calls": self.max_calls, "max_pcm_bytes": self.max_pcm_bytes, "stages_per_record": 64},
                          "observation_error_count": self.error_count, "observation_errors": list(self.errors),
                          "observation_hooks": self.observation_hooks, "observation_total_ns": self.observation_total_ns,
                          "observation_max_ns": self.observation_max_ns}
            output.mkdir(parents=True, exist_ok=False)
            (output / "pcm").mkdir()
            for row in calls:
                for label in ("submitted_pcm", "written_wav_pcm"):
                    payload = pcm.get((row["call_id"], label))
                    if payload is not None:
                        path = output / "pcm" / f"{row['call_id']}-{label}.pcm"
                        path.write_bytes(payload)
                        row[label].update(file=str(path.relative_to(output)), sha256=sha256(payload).hexdigest(), duration_seconds=len(payload) / 32000)
                row["submitted_equals_written_pcm"] = pcm.get((row["call_id"], "submitted_pcm")) == pcm.get((row["call_id"], "written_wav_pcm")) if (row["call_id"], "written_wav_pcm") in pcm else None
                for stage in row["stages"]:
                    if "end" in stage:
                        stage["wall_seconds"] = (stage["end"]["monotonic_ns"] - stage["start"]["monotonic_ns"]) / 1e9
            report["pending_native_call_ids"] = [r["call_id"] for r in calls if "native_future_done" not in r]
            report["source_sha256_after"] = {name: sha256(Path(name).read_bytes()).hexdigest() for name in self.source_sha256}
            report["source_unchanged"] = report["source_sha256_after"] == self.source_sha256
            report["limits"] = [
                "Original functions and arguments run unchanged; no mx.eval, synchronize, decoding-setting override, retry or input substitution is added.",
                "Host wall times around MLX methods are not isolated accelerator service times. Lazy work may be charged to a later method; nested stage durations overlap and must not be summed.",
                "Only diagnostic ContextVars propagate through STT pool submissions; unrelated executor tasks keep original behavior. Room identity is resolved at LocalSTT entry and can be unavailable during prewarm or direct engine calls.",
                "Cancelled caller, cancelled queued native future and eventual native completion are distinct. Missing native completion at report snapshot is pending/unavailable, not zero inference.",
                "Submitted PCM snapshot adds a copy before enqueue; actual WAV payload bytes are retained separately without changing the original write. Both are raw native-endian mono int16 at the production 16kHz boundary.",
                "Collection is bounded; dropped records/retention-limit fields mean missing diagnostics. Existing original temporary-WAV and decoder I/O remains; diagnostic PCM/report writes happen only in explicit post-launcher flush.",
                "Measured observer helper time includes lock wait, copying and record updates, but excludes some wrapper dispatch, clock and ContextVar overhead. Instrumented observations are not a causal historical latency comparison.",
                "No new worker shutdown wait is introduced. A hard process kill may prevent any report; restoring methods while native work survives can leave stages incomplete.",
            ]
            with (output / "report.json").open("x") as stream:
                stream.write(json.dumps(report, indent=2) + "\n")
            return True
        except Exception as error:
            self.flush_error = repr(error)
            return False
