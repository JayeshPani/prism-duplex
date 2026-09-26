"""Installed SDK methods, synthetic state and real timers; no models or RTC service."""
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import inspect
import json
from pathlib import Path
from types import MethodType, SimpleNamespace

from livekit.rtc.event_emitter import EventEmitter
from livekit.agents import stt
from livekit.agents.voice.agent_activity import AgentActivity
from livekit.agents.voice.audio_recognition import AudioRecognition
from livekit.agents.voice.speech_handle import SpeechHandle
from livekit.agents.voice.turn import _INTERRUPTION_DEFAULTS

BASE = Path(__file__).resolve().parent


async def run_case(label, outcome, eot_pending=False):
    loop = asyncio.get_running_loop()
    start = loop.time()
    rows = []
    pending_recognition = loop.create_future()
    def record(event, **data):
        rows.append({"event": event, "elapsed_ms": (loop.time() - start) * 1000,
                     "recognition_pending": not pending_recognition.done(), **data})
    class Sink:
        can_pause = True
        paused = False
        def pause(self):
            self.paused = True
            record("sink_pause")
        def resume(self):
            self.paused = False
            record("sink_resume")
    sink = Sink()
    emitter = EventEmitter()
    emitter.on("agent_false_interruption", lambda ev: record("agent_false_interruption", resumed=ev.resumed))
    options = dict(_INTERRUPTION_DEFAULTS, mode="vad")
    session = SimpleNamespace(_loop=loop, options=SimpleNamespace(interruption=options),
        output=SimpleNamespace(audio=sink, audio_enabled=True), agent_state="speaking",
        _aec_warmup_remaining=0, _aec_warmup_timer=None, _user_speaking_span=None,
        emit=emitter.emit, _user_input_transcribed=lambda ev: record("final_event", text=ev.transcript))
    def agent_state(value, **kwargs):
        session.agent_state=value;record("agent_state", state=value)
    session._update_agent_state=agent_state
    session._update_user_state=lambda value, **kwargs: record("user_state", state=value)
    recognition=SimpleNamespace(_current_transcript="", _audio_transcript="", _stt=object(),
        _turn_detection_mode="vad", _end_of_turn_task=None, _closing=asyncio.Event(),
        _endpointing=SimpleNamespace(overlapping=True),
        _on_end_of_speech=lambda **kwargs: record("recognition_eos"),
        _on_start_of_agent_speech=lambda **kwargs: record("recognition_agent_resumed"))
    handle=SpeechHandle.create(allow_interruptions=True)
    activity=SimpleNamespace(_session=session, stt=recognition._stt, llm=None,
        _turn_detection="vad", _rt_turn_detection_enabled=False, _rt_overlapping_speech_enabled=False,
        _interruption_by_audio_activity_enabled=True, _audio_recognition=recognition,
        _current_speech=handle, _rt_session=None, _paused_speech=None,
        _false_interruption_timer=None, _false_interruption_pending=False,
        _cancel_speech_pause_task=None, _interruption_detected=False,
        _interruption_detection_enabled=False, interruption_enabled=False,
        _user_silence_event=asyncio.Event(), min_endpointing_delay=.4,
        _on_end_of_agent_speech=lambda **kwargs: record("agent_speech_paused"))
    for name in ("_interrupt_by_audio_activity", "_pause_enabled", "_update_paused_speech",
                 "_cancel_false_interruption_timer", "_start_false_interruption_timer",
                 "_restore_paused_speech_state", "_cancel_speech_pause"):
        setattr(activity,name,MethodType(getattr(AgentActivity,name),activity))
    eot_release=asyncio.Event()
    try:
        AgentActivity.on_vad_inference_done(activity,SimpleNamespace(
            speech_duration=.6,speaking=True,raw_accumulated_silence=0))
        assert sink.paused and not handle.interrupted
        AgentActivity.on_end_of_speech(activity,SimpleNamespace(silence_duration=.45,inference_duration=0))
        AudioRecognition._run_eou_detection(recognition,None,trigger="vad")
        record("empty_transcript_eou_guard", eot_task_is_none=recognition._end_of_turn_task is None)
        assert recognition._end_of_turn_task is None
        if eot_pending:
            # A separate control isolates what the timer actually waits for.
            recognition._end_of_turn_task=asyncio.create_task(eot_release.wait())
            record("control_eot_task_opened")
        await asyncio.sleep(options["false_interruption_timeout"] + .08)
        observed={"paused":sink.paused,"handle_interrupted":handle.interrupted,
                  "recognition_pending":not pending_recognition.done(),
                  "sdk_false_interruption_pending":activity._false_interruption_pending}
        record("after_default_timeout",**observed)
        assert observed["recognition_pending"] and not observed["handle_interrupted"]
        if eot_pending:
            assert sink.paused and activity._false_interruption_pending
            eot_release.set();await recognition._end_of_turn_task;await asyncio.sleep(0)
            assert not sink.paused
        else:
            assert not sink.paused
        pending_recognition.set_result(outcome)
        record("synthetic_recognition_settled",text=outcome)
        if outcome:
            AgentActivity.on_final_transcript(activity,stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(text=outcome,language="en")]),speaking=False)
            await activity._cancel_speech_pause_task
            assert handle.interrupted
        else:
            # StreamAdapter suppresses empty text; no final callback is made.
            assert not handle.interrupted
        return {"label":label,"status":"passed","outcome":outcome,
                "eot_pending_control":eot_pending,"default_timeout_seconds":options["false_interruption_timeout"],
                "after_timeout":observed,"final_handle_interrupted":handle.interrupted,"events":rows}
    finally:
        activity._cancel_false_interruption_timer()
        if activity._cancel_speech_pause_task:
            await asyncio.gather(activity._cancel_speech_pause_task,return_exceptions=True)
        if recognition._end_of_turn_task and not recognition._end_of_turn_task.done():
            recognition._end_of_turn_task.cancel()
            await asyncio.gather(recognition._end_of_turn_task,return_exceptions=True)
        if not pending_recognition.done():pending_recognition.cancel()
        handle._mark_done()  # dummy speech has no generation/scheduler to finish it


async def main():
    output=BASE/'false-resume-reproduction.json'
    assert not output.exists()
    paths={Path(__file__),*[Path(inspect.getsourcefile(cls)) for cls in (AgentActivity,AudioRecognition,SpeechHandle,EventEmitter)]}
    source={str(p):sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"started_at_utc":datetime.now(timezone.utc).isoformat(),"status":"running",
        "scope":"Actual installed SDK interruption/EOU methods, real default2s timers, real SpeechHandle/EventEmitter, dummy session/output/state and synthetic pending recognition Future. No model, VAD inference, native audio, RTC, networking or full generation cleanup.",
        "packages":{n:version(n) for n in ('livekit','livekit-agents')},"source_sha256":source,"cases":[],
        "limits":["Synthetic pending future is not a model inference; it isolates that the SDK timer observes EOT task, not that future.","Dummy sink does not prove PCM cutoff/queue cleanup or application hold integration safety.","No historical live cause is established; live false-interruption and pause/resume events were not recorded."]}
    try:
        for label,outcome,control in [('pending_nonempty','Cancel navigation.',False),('pending_empty','',False),('open_eot_control','',True)]:
            report['cases'].append(await asyncio.wait_for(run_case(label,outcome,control),timeout=5))
        report['status']='passed'
    except BaseException as error:
        report.update(status='failed',error=repr(error));raise
    finally:
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat()
        report['sources_unchanged']=all(sha256(Path(p).read_bytes()).hexdigest()==h for p,h in source.items())
        with output.open('x') as f:json.dump(report,f,indent=2);f.write('\n')
        print(json.dumps({'status':report['status'],'cases':[{k:r[k] for k in ('label','status','after_timeout','final_handle_interrupted')} for r in report['cases']]}),flush=True)


if __name__=='__main__':
    asyncio.run(main())
