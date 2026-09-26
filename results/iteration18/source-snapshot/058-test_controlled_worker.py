"""Model-free adapter checks; real LocalSTT lifecycle with a stub engine body."""
import asyncio
from array import array
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from livekit import rtc
from livekit.agents.voice import io
from agent.pipeline import local_stt

SPEC = importlib.util.spec_from_file_location('controlled_worker_test_subject', Path(__file__).with_name('controlled_worker.py'))
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    condition = tmp_path / 'condition.json'
    condition.write_text(json.dumps({'case_id': 'delayed', 'delay_seconds': 5}))
    protocol = {'cases': [{'id': 'delayed', 'recognition_delivery_delay_seconds': 5}, {'id': 'native'}]}
    engines = []
    def make_stt():
        engine = local_stt._Engine('stub', 'stub', None)
        engines.append(engine)
        monkeypatch.setattr(local_stt._Engine, 'get', lambda *a, **k: engine)
        return local_stt.LocalSTT('stub', 'stub')
    yield SimpleNamespace(condition=condition, protocol=protocol, make_stt=make_stt,
                          context=lambda: SimpleNamespace(room=SimpleNamespace(name='car-test')))
    for engine in engines:
        engine.pool.shutdown(wait=True)


def pcm():
    return rtc.AudioFrame(array('h', [2] * 160).tobytes(), 16000, 1, 160)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Stop navigation.', ''])
async def test_delay_stays_inside_real_pending_and_follows_observed_engine(setup, monkeypatch, text):
    native_returned = asyncio.Event()
    hold_entered, release = asyncio.Event(), asyncio.Event()
    chronology = []
    async def observed_original(engine, samples):
        chronology.append('observed_engine_return');native_returned.set();return text
    async def hold(seconds):
        assert seconds == 5 and native_returned.is_set()
        chronology.append('hold');hold_entered.set();await release.wait()
    monkeypatch.setattr(local_stt._Engine, 'transcribe', observed_original)
    adapter = subject.DeliveryDelay(setup.protocol, setup.condition, setup.context, sleep=hold)
    stt = setup.make_stt();completed=[]
    stt.on('recognition_completed', lambda *v: completed.append(v))
    adapter.install(local_stt)
    task=asyncio.create_task(stt._recognize_impl(pcm(), conn_options=None))
    try:
        await asyncio.wait_for(hold_entered.wait(), 1)
        assert stt.pending_recognitions == 1 and completed == [] and not task.done()
        release.set();result=await task
        assert result.alternatives[0].text == text
        assert stt.pending_recognitions == 0 and completed == [('returned', text)]
        assert chronology == ['observed_engine_return', 'hold']
        assert adapter.calls[0]['hold_start']['pending_recognitions'] == 1
        assert adapter.calls[0]['hold_end']['pending_recognitions'] == 1
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);adapter.restore()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['native', 'hold'])
async def test_cancellation_is_not_empty_and_does_not_leak_context(setup, monkeypatch, phase):
    entered=asyncio.Event();gate=asyncio.Event()
    async def original(engine, samples):
        if phase == 'native':entered.set();await gate.wait()
        return 'Recognized text'
    async def hold(seconds):
        entered.set();await gate.wait()
    monkeypatch.setattr(local_stt._Engine,'transcribe',original)
    adapter=subject.DeliveryDelay(setup.protocol, setup.condition, setup.context, sleep=hold)
    stt=setup.make_stt();completed=[]
    stt.on('recognition_completed',lambda *v:completed.append(v));adapter.install(local_stt)
    task=asyncio.create_task(stt._recognize_impl(pcm(),conn_options=None))
    try:
        await asyncio.wait_for(entered.wait(),1);assert stt.pending_recognitions == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert stt.pending_recognitions == 0 and completed == [('cancelled',None)]
        assert adapter.calls[0]['status']=='cancelled'
        assert adapter.calls[0]['phase']==('original_engine' if phase=='native' else 'delivery_hold')
        gate.set();assert await stt._engine.transcribe(np.zeros(1))=='Recognized text'
        assert len(adapter.calls)==1
    finally:
        gate.set();task.cancel();await asyncio.gather(task,return_exceptions=True);adapter.restore()


@pytest.mark.asyncio
async def test_original_error_identity_preserved_without_hold(setup, monkeypatch):
    failure=ValueError('engine failure');holds=[]
    async def original(*args):raise failure
    async def hold(seconds):holds.append(seconds)
    monkeypatch.setattr(local_stt._Engine,'transcribe',original)
    adapter=subject.DeliveryDelay(setup.protocol,setup.condition,setup.context,sleep=hold)
    stt=setup.make_stt();completed=[];stt.on('recognition_completed',lambda *v:completed.append(v))
    adapter.install(local_stt)
    try:
        with pytest.raises(ValueError) as captured:await stt._recognize_impl(pcm(),conn_options=None)
        assert captured.value is failure and holds==[]
        assert stt.pending_recognitions==0 and completed==[('failed',None)]
        assert adapter.calls[0]['status']=='error' and 'hold_start' not in adapter.calls[0]
    finally:adapter.restore()


@pytest.mark.asyncio
async def test_binding_once_context_isolated_and_all_room_recognitions_delayed(setup, monkeypatch):
    async def original(*args):return ''
    hold_entered=asyncio.Event();release=asyncio.Event();holds=[]
    async def hold(seconds):holds.append(seconds);hold_entered.set();await release.wait()
    monkeypatch.setattr(local_stt._Engine,'transcribe',original)
    adapter=subject.DeliveryDelay(setup.protocol,setup.condition,setup.context,sleep=hold)
    first,second=setup.make_stt(),setup.make_stt();adapter.install(local_stt)
    task=asyncio.create_task(first._recognize_impl(pcm(),conn_options=None))
    try:
        await asyncio.wait_for(hold_entered.wait(),1)
        setup.condition.write_text(json.dumps({'case_id':'native','delay_seconds':0}))
        await asyncio.wait_for(second._recognize_impl(pcm(),conn_options=None),1)
        assert first.pending_recognitions==1 and second.pending_recognitions==0
        assert len(holds)==1
        release.set();await task
        await first._recognize_impl(pcm(),conn_options=None)
        assert holds==[5,5] and len(adapter.bindings)==2
        assert [c['delay_seconds'] for c in adapter.calls]==[5,0,5]
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);adapter.restore()


@pytest.mark.asyncio
async def test_whole_span_warmup_and_inherited_child_context_excluded(setup, monkeypatch):
    calls=[];holds=[]
    async def original(engine,samples):calls.append('engine');return 'span'
    monkeypatch.setattr(local_stt._Engine,'transcribe',original)
    monkeypatch.setattr(local_stt._Engine,'transcribe_blocking',lambda *a:'warmup')
    async def hold(seconds):holds.append(seconds)
    adapter=subject.DeliveryDelay(setup.protocol,setup.condition,setup.context,sleep=hold)
    stt=setup.make_stt()
    stt._segments.extend([(1,np.ones(2,dtype=np.int16)),(2,np.ones(2,dtype=np.int16))])
    adapter.install(local_stt)
    try:
        stt.warmup();assert await stt.transcribe_since(0)=='span'
        # Explicitly retain the recognition context to exercise exclusions under inheritance.
        binding=adapter._bind(stt)
        token=adapter._active.set((binding,stt,asyncio.current_task()))
        try:
            assert await stt.transcribe_since(0)=='span'
            assert await asyncio.create_task(stt._engine.transcribe(np.zeros(1)))=='span'
        finally:adapter._active.reset(token)
        assert holds==[] and adapter.calls==[] and calls==['engine']*3
    finally:adapter.restore()


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', [{'case_id':'missing','delay_seconds':5},
                                       {'case_id':'delayed','delay_seconds':0},
                                       {'case_id':'delayed','delay_seconds':True}])
async def test_bad_condition_cannot_silently_change_arm(setup,monkeypatch,condition):
    calls=[]
    async def original(*args):calls.append(1);return 'bad'
    monkeypatch.setattr(local_stt._Engine,'transcribe',original)
    setup.condition.write_text(json.dumps(condition))
    adapter=subject.DeliveryDelay(setup.protocol,setup.condition,setup.context)
    stt=setup.make_stt();adapter.install(local_stt)
    try:
        with pytest.raises(ValueError):await stt._recognize_impl(pcm(),conn_options=None)
        assert calls==[] and len(adapter.binding_errors)==1
    finally:adapter.restore()


@pytest.mark.asyncio
async def test_baseline_public_chain_forwards_audio_events_and_pause_without_gating():
    class Sink(io.AudioOutput):
        def __init__(self):
            super().__init__(label='sink',capabilities=io.AudioOutputCapabilities(pause=True),sample_rate=16000)
            self.frames=[];self.pauses=self.resumes=self.flushes=self.clears=0
        async def capture_frame(self,frame):await super().capture_frame(frame);self.frames.append(frame)
        def flush(self):super().flush();self.flushes+=1
        def clear_buffer(self):self.clears+=1;self.on_playback_finished(interrupted=True,playback_position=.01)
        def pause(self):self.pauses+=1
        def resume(self):self.resumes+=1
    sink=Sink();events=[]
    def must_not_read():raise AssertionError('baseline inspected recognition state')
    output=subject.BaselineAudioOutput(sink,current_speech=must_not_read,
        pending_recognitions=must_not_read,user_speaking=must_not_read,emit=must_not_read)
    output.on('playback_finished',events.append)
    await output.capture_frame(pcm());output.pause();output.resume();output.flush()
    output.recognition_completed('returned','Stop');output.user_state_changed()
    output.clear_buffer();playback=await asyncio.wait_for(output.wait_for_playout(),1);output.close()
    assert len(sink.frames)==1 and (sink.pauses,sink.resumes,sink.flushes,sink.clears)==(1,1,1,1)
    assert output.can_pause and output.sample_rate==16000
    assert playback.interrupted and events==[playback]
