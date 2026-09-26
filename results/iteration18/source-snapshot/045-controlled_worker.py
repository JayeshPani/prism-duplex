"""Experiment-only output-policy control and room-scoped recognition delivery hold."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
from importlib.metadata import version
import importlib.util
import json
import os
from pathlib import Path
import sys
from threading import RLock
import time
from unittest.mock import patch
from weakref import WeakKeyDictionary

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from livekit.agents.voice import io


class BaselineAudioOutput(io.AudioOutput):
    """Transparent public chain: same application hooks, no recognition gating."""
    def __init__(self, sink, *, current_speech, pending_recognitions, user_speaking,
                 emit, max_hold_seconds=30.0):
        super().__init__(label="BaselineAudioOutput", next_in_chain=sink,
                         capabilities=io.AudioOutputCapabilities(pause=True), sample_rate=sink.sample_rate)

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        await self.next_in_chain.capture_frame(frame)

    def flush(self):
        super().flush()
        self.next_in_chain.flush()

    def clear_buffer(self):
        self.next_in_chain.clear_buffer()

    def recognition_completed(self, status, text):
        pass

    def user_state_changed(self):
        pass

    def close(self):
        pass


def stamp():
    return {"monotonic_ns": time.monotonic_ns(), "unix_ns": time.time_ns()}


class DeliveryDelay:
    """Installed AFTER STTDiagnostics, before prewarm; buffer receipts until exit."""
    def __init__(self, protocol, condition_file, get_context, *, sleep=asyncio.sleep):
        self.condition_file = Path(condition_file)
        self.get_context, self.sleep = get_context, sleep
        self.expected = {}
        for case in protocol['cases']:
            name, delay = case['id'], case.get('recognition_delivery_delay_seconds', 0)
            if not isinstance(name, str) or not name or name in self.expected:
                raise ValueError('case IDs must be unique nonempty strings')
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay not in (0, 5):
                raise ValueError('recognition delivery delay must be 0 or 5 seconds')
            self.expected[name] = delay
        if not self.expected:
            raise ValueError('protocol cases cannot be empty')
        self.bindings, self.calls, self.binding_errors = [], [], []
        self._instances = WeakKeyDictionary()
        self._active = ContextVar('iteration18_room_recognition', default=None)
        self._lock, self._patches = RLock(), None

    def _bind(self, stt):
        with self._lock:
            if stt in self._instances:
                return self._instances[stt]
            try:
                raw = self.condition_file.read_bytes()
                condition = json.loads(raw)
                name, delay = condition['case_id'], condition['delay_seconds']
                if (name not in self.expected or isinstance(delay, bool)
                        or not isinstance(delay, (int, float)) or delay != self.expected[name]):
                    raise ValueError('runtime condition does not match the frozen protocol case')
                room = self.get_context().room.name
                if not isinstance(room, str) or not room:
                    raise ValueError('job room name is unavailable')
                row = {"binding_id": len(self.bindings) + 1, "case_id": name,
                       "delay_seconds": delay, "room": room, "condition": condition,
                       "condition_sha256": sha256(raw).hexdigest(), "bound_at": stamp()}
                self.bindings.append(row)
                self._instances[stt] = row
                return row
            except BaseException as error:
                self.binding_errors.append({**stamp(), "error": repr(error)})
                raise

    def install(self, local_stt):
        if self._patches is not None:
            raise RuntimeError('delivery adapter is already installed')
        original_request = local_stt.LocalSTT._recognize_impl
        original_engine = local_stt._Engine.transcribe
        original_span = local_stt.LocalSTT.transcribe_since
        stack = self._patches = ExitStack()

        @wraps(original_request)
        async def request(stt, *args, **kwargs):
            binding = self._bind(stt)
            token = self._active.set((binding, stt, asyncio.current_task()))
            try:
                return await original_request(stt, *args, **kwargs)
            finally:
                self._active.reset(token)

        @wraps(original_engine)
        async def engine(engine, *args, **kwargs):
            active = self._active.get()
            if (active is None or engine is not active[1]._engine
                    or asyncio.current_task() is not active[2]):
                return await original_engine(engine, *args, **kwargs)
            binding, stt, _ = active
            with self._lock:
                row = {"call_id": len(self.calls) + 1, "binding_id": binding['binding_id'],
                       "case_id": binding['case_id'], "room": binding['room'],
                       "delay_seconds": binding['delay_seconds'], "entry": stamp()}
                self.calls.append(row)
            phase = 'original_engine'
            try:
                # Diagnostics around the engine return before our synthetic delivery wait.
                result = await original_engine(engine, *args, **kwargs)
                row['original_engine_return'] = stamp()
                if binding['delay_seconds']:
                    phase = 'delivery_hold'
                    row['hold_start'] = {**stamp(), 'pending_recognitions': stt.pending_recognitions}
                    await self.sleep(binding['delay_seconds'])
                    row['hold_end'] = {**stamp(), 'pending_recognitions': stt.pending_recognitions}
                row['status'] = 'returned'
                return result
            except BaseException as error:
                row.update(status='cancelled' if isinstance(error, asyncio.CancelledError) else 'error',
                           phase=phase, error=repr(error))
                raise
            finally:
                row['delivery_return'] = stamp()
        @wraps(original_span)
        async def whole_span(*args, **kwargs):
            token = self._active.set(None)
            try:
                return await original_span(*args, **kwargs)
            finally:
                self._active.reset(token)

        stack.enter_context(patch.object(local_stt.LocalSTT, 'transcribe_since', whole_span))
        stack.enter_context(patch.object(local_stt.LocalSTT, '_recognize_impl', request))
        stack.enter_context(patch.object(local_stt._Engine, 'transcribe', engine))

    def restore(self):
        if self._patches is not None:
            self._patches.close()
            self._patches = None


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', choices=('baseline', 'candidate'), required=True)
    parser.add_argument('--condition-file', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, default=Path(__file__).with_name('protocol.json'))
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    directory = args.manifest.parent
    paths = {name: directory / name for name in ('experiment-worker-selection.json',
        'recognition-delivery-diagnostics.json', 'stt-diagnostics-exit.json')}
    if (args.manifest.exists() or any(p.exists() for p in paths.values())
            or (directory / 'stt-diagnostics').exists()
            or (directory / 'aec-discard-diagnostics.json').exists()):
        parser.error('worker outputs already exist; use a fresh output directory')
    protocol = json.loads(args.protocol.read_bytes())
    os.environ.setdefault('PRISM_PROFILE', 'local-mac')
    os.environ['HF_HUB_OFFLINE'] = '1'
    from livekit.agents import get_job_context
    from parakeet_mlx import parakeet
    from agent.pipeline import local_stt
    from agent import main as prism

    diagnostic_path = ROOT / 'results/iteration12/stt_diagnostics.py'
    aec_path = ROOT / 'results/iteration11/instrumented_worker.py'
    diagnostics = load_module('iteration18_stt_diagnostics', diagnostic_path).STTDiagnostics(get_job_context)
    launcher = load_module('iteration18_aec_worker', aec_path)
    delay = DeliveryDelay(protocol, args.condition_file, get_job_context)
    sources = [Path(__file__), diagnostic_path, aec_path, ROOT / 'scripts/local_livekit_worker.py',
               Path(local_stt.__file__), Path(prism.__file__),
               ROOT / 'agent/pipeline/recognition_pause.py', args.protocol]
    hashes = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in sources}
    selection = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'policy': args.policy,
        'condition_file': str(args.condition_file.resolve()), 'protocol': str(args.protocol.resolve()),
        'source_sha256': hashes,
        'packages': {n: version(n) for n in ('livekit', 'livekit-agents')},
        'method': 'STTDiagnostics installed first, then context-scoped Engine.transcribe delivery hold, then unchanged iteration11 AEC observer/worker. Only baseline replaces the application output symbol with a transparent public AudioOutput chain.',
        'limits': ['Delay applies to every successful room recognition including initial and empty text in the bound case; never selects on transcript or PCM.',
                   'Engine/native failure or cancellation is re-raised without adding a delay. Cancellation during synthetic hold remains cancellation.',
                   'Warmup, direct engine calls and whole-span transcribe_since are excluded. Pending LocalSTT count includes the hold.',
                   'Condition is read once per LocalSTT object at its first recognition. Serial fresh-room/departure ordering is a suite responsibility; every observed binding retains the exact condition hash.',
                   'No live diagnostic writes after launch selection. SIGKILL can leave buffered reports unavailable.',
                   'The baseline shares current counters/SDK false-event logging; it is an output-policy control, not a replay of historical source.']}
    directory.mkdir(parents=True, exist_ok=True)
    with paths['experiment-worker-selection.json'].open('x') as f:
        json.dump(selection, f, indent=2); f.write('\n')
    status = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'starting'}
    original_argv = sys.argv[:]
    try:
        with ExitStack() as stack:
            diagnostics.install(local_stt, parakeet)
            delay.install(local_stt)
            if args.policy == 'baseline':
                stack.enter_context(patch.object(prism, 'RecognitionAwareAudioOutput', BaselineAudioOutput))
            sys.argv = [original_argv[0], '--manifest', str(args.manifest)]
            launcher.main()
            status['status'] = 'launcher_returned'
    except BaseException as error:
        status.update(status='launcher_exited', exception=repr(error))
        raise
    finally:
        sys.argv = original_argv
        delay.restore()
        diagnostics.restore()
        status['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report = {**status, 'policy': args.policy, 'source_sha256': hashes,
            'source_hashes_unchanged': all(sha256(Path(p).read_bytes()).hexdigest() == h for p, h in hashes.items()),
            'bindings': delay.bindings, 'calls': delay.calls, 'binding_errors': delay.binding_errors}
        with paths['recognition-delivery-diagnostics.json'].open('x') as f:
            json.dump(report, f, indent=2); f.write('\n')
        saved = diagnostics.flush(directory / 'stt-diagnostics')
        with paths['stt-diagnostics-exit.json'].open('x') as f:
            json.dump({**status, 'diagnostics_saved': saved, 'diagnostic_error': diagnostics.flush_error}, f, indent=2)
            f.write('\n')


if __name__ == '__main__':
    main()
