"""Freeze methods, tested sources, runtime sources and observed model references."""
import ast
from importlib.metadata import version
import importlib.util
import json
from pathlib import Path
import platform
import socket
import sys

from common import BASE, ROOT, digest, save, stamp
sys.path.insert(0,str(ROOT))


def main():
    assert not (BASE/'audio-assets').exists(), 'Must freeze before first render'
    assert not (BASE/'preflight.json').exists()
    report=dict(status='running',started_at_utc=stamp(),source_sha256={},model_files={},issues=[])
    save(BASE/'preflight.json',report,exclusive=True)
    try:
        tested=json.loads((BASE/'tested-source-v2.json').read_text())
        assert tested['exit_code']==0 and tested['unchanged_during_test']
        assert digest(BASE/'full-tests-v2.txt')==tested['log_sha256']
        assert all(digest(ROOT/name)==value for name,value in tested['paths_sha256'].items())
        methods=list(BASE.glob('*.py'))
        assert all((BASE/n).is_file() for n in ('controlled_worker.py','audit_acoustic.py','make_inputs.py','run_suite.py','supervise.py','review_received_audio.py','verify_after_inference.py'))
        for p in methods:ast.parse(p.read_text(),filename=str(p))
        prior=json.loads((ROOT/'results/iteration17/post-inference-validation.json').read_text())
        assert prior['status']=='passed' and len(prior['model_files'])==10
        for name,row in prior['model_files'].items():
            expected=row['expected']; observed=digest(name)
            report['model_files'][name]=dict(reference=expected,observed=observed,bytes=Path(name).stat().st_size)
            assert expected==observed,name
        for port,kind in ((7880,socket.SOCK_STREAM),(7882,socket.SOCK_DGRAM),(8081,socket.SOCK_STREAM),(8082,socket.SOCK_STREAM)):
            with socket.socket(socket.AF_INET,kind) as s:s.bind(('127.0.0.1',port))
        from livekit.agents.voice import agent_activity, agent_session, audio_recognition, generation, io, room_io, speech_handle, turn
        from livekit.agents.voice.transcription import synchronizer
        from livekit.agents.stt import stream_adapter
        import mlx_lm.server
        runtime=[Path(x.__file__).resolve() for x in (agent_activity,agent_session,audio_recognition,generation,io,room_io._output,speech_handle,turn,synchronizer,stream_adapter,mlx_lm.server)]
        report['required_runtime_sources']=[str(p) for p in runtime]
        assert len(runtime)==11
        paths=set([ROOT/name for name in tested['paths_sha256']]+methods+runtime)
        paths.update(BASE/n for n in ('protocol.json','tested-source-v2.json','full-tests-v2.txt','received-audio-review-protocol.json'))
        paths.update(ROOT/n for n in ('scripts/local_speech_experiment.py','scripts/local_livekit_worker.py','results/iteration11/instrumented_worker.py','results/iteration12/stt_diagnostics.py','results/iteration17/verify_after_inference.py','results/iteration16/local-stack-run1/llm-launch.json','results/iteration16/local-stack-run1/livekit.yaml'))
        for arm in ('baseline','candidate'):
            paths.update(ROOT/'results'/f'iteration18-{arm}'/n for n in ('plan.json','protocol.json'))
        snapshot=BASE/'source-snapshot';snapshot.mkdir(exist_ok=False)
        rows=[]
        for index,path in enumerate(sorted(paths)):
            raw=path.read_bytes();h=digest(path)
            relative=str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else None
            if relative in tested['paths_sha256']:
                assert h==tested['paths_sha256'][relative], f'Tested source changed before freeze: {relative}'
            target=snapshot/f'{index:03d}-{path.name}';target.write_bytes(raw)
            assert digest(target)==h
            report['source_sha256'][str(path)]=h
            rows.append(dict(original=str(path),snapshot=str(target),sha256=h))
        save(BASE/'source-snapshot.json',dict(created_at_utc=stamp(),files=rows),exclusive=True)
        report.update(status='passed',python=sys.version,platform=platform.platform(),snapshot_sha256=digest(BASE/'source-snapshot.json'),
                      packages={n:version(n) for n in ('mlx','mlx-lm','parakeet-mlx','kokoro-onnx','onnxruntime','livekit-agents','livekit','livekit-api','numpy')},
                      tests='696 passed in5.87s; 79source paths unchanged',limits='Observed reference matches, not publisher authentication. Methods frozen before first new input render; full model bytes checked again only after all stages.')
    except BaseException as error:
        report.update(status='failed',error=repr(error));raise
    finally:
        report['finished_at_utc']=stamp();save(BASE/'preflight.json',report)
    print('Preflight passed:',len(report['source_sha256']),'source/method/runtime files and',len(report['model_files']),'model references')

if __name__=='__main__':main()
