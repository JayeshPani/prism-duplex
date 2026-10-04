"""Freeze tested candidate, isolated baseline, methods and observed model bytes."""
import ast
from importlib.metadata import distribution, version
import json
from pathlib import Path
import platform
import socket
import sys

from common import BASE, ROOT, digest, save, stamp


def main():
    target = BASE / 'preflight.json'
    assert not (BASE / 'audio-assets').exists(), 'Freeze must precede first render'
    report = dict(status='running', started_at_utc=stamp(), source_sha256={}, model_files={}, issues=[])
    save(target, report, exclusive=True)
    try:
        tested = json.loads((BASE / 'tested-source-v2.json').read_text())
        assert tested['exit_code'] == 0 and tested['unchanged_during_test']
        assert digest(BASE / 'full-tests-v2.txt') == tested['log_sha256']
        assert all(digest(ROOT / p) == h for p, h in tested['paths_sha256'].items())
        methods = sorted(BASE.glob('*.py'))
        for p in methods:
            ast.parse(p.read_text(), filename=str(p))
        required = {'controlled_worker.py', 'audit_acoustic.py', 'make_inputs.py', 'run_suite.py',
                    'supervise.py', 'review_received_audio.py', 'verify_after_inference.py'}
        assert required <= {p.name for p in methods}
        prior = json.loads((ROOT / 'results/iteration18/post-inference-validation.json').read_text())
        assert prior['status'] == 'passed' and prior['all_phases_reached'] and not prior['issues']
        assert len(prior['model_files']) == 10
        for port, kind in ((7880, socket.SOCK_STREAM), (7882, socket.SOCK_DGRAM),
                           (8081, socket.SOCK_STREAM), (8082, socket.SOCK_STREAM)):
            with socket.socket(socket.AF_INET, kind) as s:
                s.bind(('127.0.0.1', port))
        for name, row in prior['model_files'].items():
            observed = digest(name)
            report['model_files'][name] = dict(reference=row['expected'], observed=observed,
                                              bytes=Path(name).stat().st_size)
            assert observed == row['expected'], name
        old_freeze = json.loads((ROOT / 'results/iteration18/preflight.json').read_text())
        runtime = [Path(p) for p in old_freeze['required_runtime_sources']]
        assert len(runtime) == 11
        assert all(digest(p) == old_freeze['source_sha256'][str(p)] for p in runtime)
        report['required_runtime_sources'] = [str(p) for p in runtime]
        # The baseline is a separate importable package with the five original
        # modules restored, not a mutation of the user's working source.
        baseline = BASE / 'baseline-package/agent'
        assert baseline.is_dir()
        original = json.loads((BASE / 'baseline.json').read_text())['baseline_sources']
        for relative, expected in original.items():
            assert digest(BASE / 'baseline-source' / relative) == expected
            assert digest(BASE / 'baseline-package' / relative) == expected
        baseline_paths = {p for p in baseline.rglob('*') if p.is_file()
                          and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        for p in baseline_paths:
            relative = str(p.relative_to(BASE / 'baseline-package'))
            if relative not in original:
                assert digest(p) == digest(ROOT / relative), relative
        paths = {ROOT / p for p in tested['paths_sha256']} | set(methods) | set(runtime) | baseline_paths
        sdk = distribution('livekit-agents')
        paths.update(Path(sdk.locate_file(name)).resolve() for name in (
            'livekit/agents/worker.py', 'livekit/agents/ipc/job_thread_executor.py'))
        paths.update(BASE / n for n in ('protocol.json', 'source-preparation.json', 'baseline.json',
                                        'tested-source-v2.json', 'full-tests-v2.txt',
                                        'received-audio-review-protocol.json'))
        paths.update((BASE / 'baseline-source').rglob('*.py'))
        paths.update(ROOT / n for n in (
            'scripts/local_speech_experiment.py', 'scripts/local_livekit_worker.py',
            'results/iteration11/instrumented_worker.py', 'results/iteration12/stt_diagnostics.py',
            'results/iteration17/verify_after_inference.py',
            'results/iteration16/local-stack-run1/llm-launch.json',
            'results/iteration16/local-stack-run1/livekit.yaml'))
        for arm in ('baseline', 'candidate'):
            paths.update(ROOT / 'results' / f'iteration19-{arm}' / n for n in ('plan.json', 'protocol.json'))
        snapshot = BASE / 'source-snapshot'
        snapshot.mkdir(exist_ok=False)
        rows = []
        for index, path in enumerate(sorted(paths)):
            raw, expected = path.read_bytes(), digest(path)
            destination = snapshot / f'{index:03d}-{path.name}'
            destination.write_bytes(raw)
            assert digest(destination) == expected
            report['source_sha256'][str(path)] = expected
            rows.append(dict(original=str(path), snapshot=str(destination), sha256=expected))
        save(BASE / 'source-snapshot.json', dict(created_at_utc=stamp(), files=rows), exclusive=True)
        assert all(digest(p) == h for p, h in report['source_sha256'].items())
        report.update(status='passed', python=sys.version, platform=platform.platform(),
                      snapshot_sha256=digest(BASE / 'source-snapshot.json'),
                      packages={n: version(n) for n in ('mlx', 'mlx-lm', 'parakeet-mlx', 'kokoro-onnx',
                                'onnxruntime', 'livekit-agents', 'livekit', 'livekit-api', 'numpy')},
                      test_receipt_sha256=digest(BASE / 'tested-source-v2.json'),
                      limits='Observed model reference matches, not publisher authentication. Frozen before render; full model bytes rechecked only after all inference stops.')
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        report['finished_at_utc'] = stamp()
        save(target, report)
    print(json.dumps(dict(status=report['status'], files=len(report['source_sha256']), models=len(report['model_files']))))


if __name__ == '__main__':
    main()
