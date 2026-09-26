"""First local TTS renders and predetermined synthetic controls; no selection."""
import json
import os
from pathlib import Path
import signal
import sys
import time
import traceback
import wave

from common import BASE, ROOT, digest, save, stamp, verify_frozen
sys.path.insert(0, str(ROOT))


def main():
    frozen = verify_frozen()
    protocol = json.loads((BASE / 'protocol.json').read_text())
    creation = protocol['source_audio_creation']
    out = BASE / 'audio-assets'
    out.mkdir(exist_ok=False)
    evidence = BASE / 'input-generation'
    evidence.mkdir(exist_ok=False)
    os.environ.update(HF_HUB_OFFLINE='1', HF_DATASETS_OFFLINE='1', PRISM_MODEL_RECEIPTS_DIR=str(evidence / 'model-receipts'))
    report = dict(status='running', started_at_utc=stamp(), pid=os.getpid(), command=sys.argv,
                  method_sha256=digest(__file__), preflight_sha256=digest(BASE / 'preflight.json'),
                  rows=[], cleanup=[], limits='First renders retained untrimmed. Synthetic controls are not human breath or road noise. No preparation ASR or VAD selection.')
    target = evidence / 'report.json'
    save(target, report, exclusive=True)
    tts = None
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        import numpy as np
        from agent.config import load_config
        from agent.pipeline.local_tts import KokoroTTS
        from scripts.local_speech_experiment import write_wav
        cfg = load_config('local-mac')
        assert cfg['tts'] == protocol['configuration']['tts']
        report['numpy_version'] = np.__version__
        tts = KokoroTTS(voice=cfg['tts']['voice'], speed=cfg['tts']['speed'])
        before = time.monotonic(); tts.load(); report['load_seconds'] = time.monotonic() - before
        pcm = {}
        def retain(name, samples, **metadata):
            samples = np.asarray(samples, dtype=np.int16)
            path = out / f'{name}.wav'
            assert not path.exists()
            write_wav(path, samples.tobytes(), 24000)
            row = dict(id=name, status='completed', asset=path.name, samples=len(samples), duration_seconds=len(samples)/24000,
                       sha256=digest(path), peak_pcm=int(np.abs(samples.astype(np.int32)).max(initial=0)), **metadata)
            report['rows'].append(row); save(target, report)
            pcm[name] = samples
        for name, text in creation['spoken_stems'].items():
            start = time.monotonic()
            raw = tts.render(text)
            assert len(raw) % 2 == 0
            retain(name, np.frombuffer(raw, dtype=np.int16), text=text, tts_seconds=time.monotonic()-start)
        for ms, n in ((250,6000),(1200,28800)):
            prefix, tail = pcm['pause_prefix'], pcm['metro_tail']
            active_a = np.flatnonzero(np.abs(prefix.astype(np.float64))/32768 >= 10**(-40/20))
            active_b = np.flatnonzero(np.abs(tail.astype(np.float64))/32768 >= 10**(-40/20))
            gap = (len(prefix)+n+int(active_b[0])-int(active_a[-1])-1)/24000 if len(active_a) and len(active_b) else None
            retain(f'pause_metro_{ms}', np.concatenate((prefix,np.zeros(n,dtype=np.int16),tail)),
                   inserted_silence_samples=n, prefix_samples=len(prefix), tail_first_activity_sample=int(active_b[0]) if len(active_b) else None,
                   tail_activity_start_sample=len(prefix)+n+int(active_b[0]) if len(active_b) else None,
                   activity_threshold_dbfs=-40, measured_sample_activity_gap_seconds=gap,
                   activity_limit='Single-sample threshold on untrimmed stems, not VAD or human speech endpoint.')
        retain('silence900', np.zeros(21600,dtype=np.int16))
        for name, seed, low, high in (('noise_burst900',1802,300,3400),('breath_proxy900',1803,500,6000)):
            n=21600
            noise=np.random.Generator(np.random.PCG64(seed)).standard_normal(n)
            bins=np.fft.rfftfreq(n,1/24000)
            spectrum=np.fft.rfft(noise); spectrum[(bins<low)|(bins>high)]=0
            samples=np.fft.irfft(spectrum,n=n)
            if name=='noise_burst900':
                env=np.ones(n); env[:480]=np.linspace(0,1,480); env[-480:]=np.linspace(1,0,480)
            else:
                env=np.concatenate(((1-np.cos(np.linspace(0,np.pi,4320)))/2,
                                    (1+np.cos(np.linspace(0,np.pi,17280)))/2))
            samples*=env
            samples*=10**(-24/20)/np.sqrt(np.mean(samples*samples))
            quantized=np.rint(samples*32768)
            clipping=int(np.count_nonzero((quantized < -32768)|(quantized>32767)))
            if clipping:
                report['rows'].append(dict(id=name,status='generation_failed',clipping_count=clipping)); save(target,report)
                raise RuntimeError(f'{name}: predetermined control would clip')
            retain(name,quantized.astype(np.int16),seed=seed,clipping_count=clipping,rms_dbfs=float(20*np.log10(np.sqrt(np.mean(samples*samples)))))
        expected={i['asset'] for c in protocol['cases'] for i in c['inputs']}
        assert expected.issubset({r['asset'] for r in report['rows']})
        report['source_unchanged']=verify_frozen()['source_sha256']==frozen['source_sha256']
        save(BASE/'input-assets.json', dict(frozen_at_utc=stamp(), assets={r['asset']:r for r in report['rows']},
             protocol_sha256=digest(BASE/'protocol.json'), preparation_asr='not_run',preparation_vad='not_run'), exclusive=True)
        report['status']='completed'
    except BaseException as error:
        report.update(status='failed',error=repr(error),traceback=traceback.format_exc())
    finally:
        if tts is not None:
            try:
                tts._pool.shutdown(wait=True); report['cleanup'].append(dict(pool='tts',status='completed'))
            except BaseException as error:
                report.update(status='failed',cleanup_error=repr(error))
        report['finished_at_utc']=stamp(); save(target,report)
    print(json.dumps({k:report[k] for k in ('status','rows','cleanup')}),flush=True)
    return int(report['status']!='completed')

if __name__=='__main__':
    raise SystemExit(main())
