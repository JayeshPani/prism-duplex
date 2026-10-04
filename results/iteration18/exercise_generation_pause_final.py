"""Real SDK generation/room forwarding; no models, RTC service or native audio device."""
from __future__ import annotations

import asyncio
from array import array
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import inspect
import json
from pathlib import Path
import sys
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from livekit import rtc
from livekit.agents import Agent, AgentSession
from livekit.agents.voice.agent_activity import AgentActivity
from livekit.agents.voice.generation import perform_audio_forwarding
from livekit.agents.voice.room_io._output import _ParticipantAudioOutput
from livekit.agents.voice.speech_handle import SpeechHandle
from agent.pipeline.recognition_pause import RecognitionAwareAudioOutput

BASE = Path(__file__).resolve().parent / "generation-pause-final"


async def until(predicate, *, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.001)


def frame(tag):
    return rtc.AudioFrame(array('h', [tag] * 800).tobytes(), 16000, 1, 800)


async def run_case(label):
    loop = asyncio.get_running_loop()
    started = loop.time()
    events = []
    frames = []
    state = {'pending': 0, 'speaking': False}
    row = {'label': label, 'status': 'running', 'events': events, 'source_frames': frames}
    def record(event, **data):
        events.append({'event': event, 'elapsed_ms': 1000 * (loop.time() - started), **data})
    class FakeAudioSource:
        queued_duration = 0.0
        def __init__(self, *args, **kwargs):
            record('fake_audio_source_created', queue_size_ms=kwargs.get('queue_size_ms'))
        async def capture_frame(self, audio_frame):
            tag = array('h', audio_frame.data)[0]
            frames.append({'tag': tag, 'elapsed_ms': 1000 * (loop.time() - started),
                           'sha256': sha256(bytes(audio_frame.data)).hexdigest()})
            record('native_boundary_capture', tag=tag)
            await asyncio.sleep(0)
        def clear_queue(self):
            record('native_boundary_clear')
        async def wait_for_playout(self):
            record('native_boundary_playout')
            await asyncio.sleep(0)
        async def aclose(self):
            record('fake_audio_source_closed')

    session = AgentSession(vad=None, stt=None, llm=None, tts=None,
        turn_handling={'turn_detection': 'manual', 'interruption': {
            'enabled': True, 'mode': 'vad', 'resume_false_interruption': True}},
        aec_warmup_duration=0, user_away_timeout=None)
    with patch.object(rtc, 'AudioSource', FakeAudioSource):
        sink = _ParticipantAudioOutput(None, sample_rate=16000, num_channels=1,
                                       track_publish_options=rtc.TrackPublishOptions())
    # No room/publication: mark subscription available and run the installed forwarder unchanged.
    sink._subscribed_fut.set_result(None)
    sink._forwarding_task = asyncio.create_task(sink._forward_audio())
    session.output.audio = sink
    sink.on('playback_finished', lambda ev: record('playback_finished', interrupted=ev.interrupted,
                                                  playback_position=ev.playback_position))
    session.on('agent_state_changed', lambda ev: record('agent_state', state=ev.new_state))
    handle = None
    wrapper = None
    gate = asyncio.Event()
    try:
        await session.start(Agent(instructions='Component test; supplied audio only.'), record=False)
        if label != 'event_only_control':
            wrapper = RecognitionAwareAudioOutput(sink, current_speech=lambda: session.current_speech,
                pending_recognitions=lambda: state['pending'], user_speaking=lambda: state['speaking'],
                emit=record, max_hold_seconds=.15 if label == 'timeout' else 3.0)
            session.output.audio = wrapper
        output = session.output.audio
        async def audio():
            try:
                yield frame(1)
                await gate.wait()
                yield frame(2)
                yield frame(3)
            finally:
                record('supplied_audio_closed')
        handle = session.say('Old result.', audio=audio(), allow_interruptions=True)
        handle.add_done_callback(lambda h: record('handle_done', interrupted=h.interrupted,
                                                 error=repr(h.exception()) if h.exception() else None))
        await until(lambda: len(frames) == 1 and session.current_speech is handle)
        state['pending'] = 1
        output.pause()
        record('mock_vad_pause', handle_id=handle.id)
        gate.set()
        # Ensure the actual room forwarder has dequeued an old frame and is waiting on its gate.
        await until(lambda: not sink._forwarding_idle.is_set() and not sink._playback_enabled.is_set())
        assert [f['tag'] for f in frames] == [1]
        record('forward_waiter_parked')
        if label == 'event_only_control':
            output.resume()
            output.pause()  # synchronous event callback is still too late to retract wakeup
            await until(lambda: any(f['tag'] == 2 for f in frames))
            row['leaked_while_gate_cleared'] = not sink._playback_enabled.is_set()
            assert row['leaked_while_gate_cleared']
            record('event_only_leak_observed')
            handle.interrupt()
            await asyncio.wait_for(handle.wait_for_playout(), 2)
            sink.resume()
        else:
            # Models/VAD are absent: invoke exactly the public output command the SDK timer uses.
            output.resume()
            output.resume()  # a second SDK resume must not bypass the owned hold
            await asyncio.sleep(.03)
            assert [f['tag'] for f in frames] == [1]
            assert not sink._playback_enabled.is_set() and not handle.done()
            record('repeated_resume_blocked')
            if label == 'timeout':
                await until(lambda: handle.interrupted)
            else:
                state['pending'] = 0
                if label == 'empty':
                    output.recognition_completed('returned', '')
                elif label == 'error':
                    output.recognition_completed('failed', None)
                else:
                    output.recognition_completed('returned', 'Cancel navigation.')
                record('mock_recognition_completed', kind=label)
                if label != 'empty':
                    # Exercise the other SDK resume call during generation cleanup.
                    output.resume()
                    assert [f['tag'] for f in frames] == [1]
            await asyncio.wait_for(handle.wait_for_playout(), 2)
            await asyncio.sleep(0)
            assert handle.exception() is None
            if label == 'empty':
                assert not handle.interrupted
                assert [f['tag'] for f in frames] == [1, 2, 3]
            else:
                assert handle.interrupted
                assert [f['tag'] for f in frames] == [1]
            row['old_handle_interrupted'] = handle.interrupted
            row['old_handle_done'] = handle.done()
            row['old_native_tags_after_completion'] = [f['tag'] for f in frames]
            state['pending'] = 0
            async def replacement_audio():
                yield frame(9)
            replacement = session.say('Replacement result.', audio=replacement_audio(), allow_interruptions=True)
            await asyncio.wait_for(replacement.wait_for_playout(), 2)
            assert replacement.done() and not replacement.interrupted and replacement.exception() is None
            assert frames[-1]['tag'] == 9
            row['replacement_completed'] = True
        row['status'] = 'passed'
    except BaseException as error:
        row.update(status='failed', error=repr(error), traceback=traceback.format_exc())
    finally:
        gate.set()
        if handle is not None and not handle.done():
            handle.interrupt(force=True)
        if wrapper is not None:
            wrapper.close()
        sink.resume()  # teardown only; after observations and assertions
        try:
            await asyncio.wait_for(session.aclose(), 3)
            await asyncio.wait_for(sink.aclose(), 3)
            row['cleanup'] = 'completed'
        except BaseException as error:
            row.update(cleanup='failed', cleanup_error=repr(error), status='failed')
        row['elapsed_seconds'] = loop.time() - started
    return row


async def main():
    output = BASE / 'generation-pause-component.json'
    assert not output.exists(), 'Refusing to overwrite prior result'
    paths = {Path(__file__), Path(inspect.getsourcefile(RecognitionAwareAudioOutput)),
             *[Path(inspect.getsourcefile(c)) for c in (AgentSession, AgentActivity, SpeechHandle,
                _ParticipantAudioOutput, perform_audio_forwarding)]}
    source = {str(p): sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'running',
        'packages': {n: version(n) for n in ('livekit-agents', 'livekit')},
        'source_sha256': source, 'cases': [],
        'scope': 'Actual public AgentSession.start/say/audio and SDK generation/SpeechHandle completion; installed private RoomIO forwarding/playout methods unchanged. Fake native AudioSource and subscription setup, injected pause/resume and recognition outcomes. No models, VAD inference, STT, network, RTC service, native audio playback or physical audible-latency claim.',
        'limits': ['Fake AudioSource has zero queued duration; native buffering, partial device playback and real RTC transport are not exercised.',
                   'No live VAD/false-interruption timer is used here; the previous default-timer reproduction covers that separate boundary.',
                   'This harness never marks SpeechHandle done manually; actual SDK generation/scheduling owns completion.',
                   'Small deterministic component cases do not establish behavior under all concurrent room/recognition/session transitions.']}
    try:
        for label in ('event_only_control', 'nonempty', 'empty', 'error', 'timeout'):
            report['cases'].append(await asyncio.wait_for(run_case(label), 12))
        report['status'] = 'passed' if all(r['status'] == 'passed' for r in report['cases']) else 'failed'
    except BaseException as error:
        report.update(status='failed', error=repr(error), traceback=traceback.format_exc())
    finally:
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['sources_unchanged'] = all(sha256(Path(p).read_bytes()).hexdigest() == h for p, h in source.items())
        with output.open('x') as f:
            json.dump(report, f, indent=2); f.write('\n')
        print(json.dumps({'status': report['status'], 'sources_unchanged': report['sources_unchanged'],
                          'cases': [{k: r.get(k) for k in ('label', 'status', 'error', 'cleanup',
                            'old_native_tags_after_completion', 'old_handle_interrupted', 'replacement_completed')}
                                    for r in report['cases']]}), flush=True)
    return 0 if report['status'] == 'passed' and report['sources_unchanged'] else 1

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
