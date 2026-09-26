"""First untrimmed local TTS renders for matched clarification cases; no selection."""
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
                  rows=[], cleanup=[], limits='First renders retained untrimmed and shared across both arms. Known wording is development exposure. No preparation ASR or VAD selection.')
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
