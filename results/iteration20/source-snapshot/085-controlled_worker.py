"""Select a complete frozen agent package; no policy patch or recognition hold."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys

from common import BASE, ROOT, digest, save, verify_frozen


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def loaded_agent_modules(package_root, frozen):
    rows, errors = {}, []
    for name, module in sorted(sys.modules.items()):
        if name != 'agent' and not name.startswith('agent.'):
            continue
        file = getattr(module, '__file__', None)
        if not file:
            errors.append({'module': name, 'error': 'missing source path'})
            continue
        path = Path(file).resolve()
        value = digest(path)
        rows[name] = {'path': str(path), 'sha256': value}
        if not path.is_relative_to(package_root / 'agent') or frozen['source_sha256'].get(str(path)) != value:
            errors.append({'module': name, 'path': str(path), 'error': 'wrong package or source absent/mismatched in preflight'})
    return rows, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', choices=('baseline', 'candidate'), required=True)
    parser.add_argument('--protocol', type=Path, default=BASE / 'protocol.json')
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    frozen = verify_frozen()
    plan_path = ROOT / 'results' / f'iteration20-{args.policy}' / 'plan.json'
    plan = json.loads(plan_path.read_text())
    protocol = json.loads(args.protocol.read_text())
    package_root = Path(plan['agent_package_root']).resolve()
    assert package_root == (BASE / 'baseline-package' if args.policy == 'baseline' else ROOT)
    assert not any(name == 'agent' or name.startswith('agent.') for name in sys.modules), 'Agent imported before source selection'
    directory = args.manifest.parent
    selection_path, exit_path = directory / 'experiment-worker-selection.json', directory / 'experiment-worker-exit.json'
    assert not any(p.exists() for p in (args.manifest, selection_path, exit_path, directory / 'stt-diagnostics',
                                       directory / 'aec-discard-diagnostics.json', directory / 'stt-diagnostics-exit.json'))
    diagnostic_path = ROOT / 'results/iteration12/stt_diagnostics.py'
    aec_path = ROOT / 'results/iteration11/instrumented_worker.py'
    observer = load_module('iteration20_stt_diagnostics', diagnostic_path)
    launcher = load_module('iteration20_aec_worker', aec_path)
    # The legacy observer inserts ROOT, so make selection last before any agent import.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(package_root))
    os.environ.setdefault('PRISM_PROFILE', 'local-mac')
    os.environ['HF_HUB_OFFLINE'] = '1'
    from livekit.agents import get_job_context
    from parakeet_mlx import parakeet
    from agent.pipeline import local_stt, local_tts
    from agent import main as prism
    from agent.config import local_turn_handling, room_mode
    from scripts import local_livekit_worker
    assert prism.CFG == protocol['configuration'], 'Selected runtime configuration differs from frozen protocol'
    assert room_mode('car-audio-iteration20') == 'car'
    assert local_turn_handling() == protocol['turn_handling'], 'Selected VAD/endpointing/interruption differs'
    # Package relocation changes the default resource path. Both arms use the
    # same frozen model files, independently of their source-package location.
    original_tts_directory = local_tts.MODELS_DIR
    local_tts.MODELS_DIR = ROOT / 'models/kokoro'
    tts_references = {str(local_tts.MODELS_DIR / name): frozen['model_files'][str(local_tts.MODELS_DIR / name)]['reference']
                      for name in ('kokoro-v1.0.onnx', 'voices-v1.0.bin')}
    loaded, errors = loaded_agent_modules(package_root, frozen)
    assert not errors, errors
    # This legacy launcher's ROOT only selects source hashes inside main().
    # Point that inventory at the package actually imported; code on disk is unchanged.
    manifest_root = local_livekit_worker.ROOT
    local_livekit_worker.ROOT = package_root
    sys.path.insert(0, str(package_root))
    selection = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'policy': args.policy,
                 'source_variant': plan['source_variant'], 'agent_package_root': str(package_root),
                 'configuration': prism.CFG, 'turn_handling': local_turn_handling(), 'mode': 'car',
                 'loaded_agent_modules': loaded, 'plan_sha256': digest(plan_path), 'protocol_sha256': digest(args.protocol),
                 'source_sha256': {str(p): digest(p) for p in (Path(__file__).resolve(), diagnostic_path, aec_path,
                     ROOT / 'scripts/local_livekit_worker.py', Path(local_stt.__file__), Path(prism.__file__))},
                 'method': 'Complete agent package selected before agent import; STTDiagnostics and unchanged iteration11 AEC observer only. Both policies use RecognitionAwareAudioOutput with no substitution or injected delay.',
                 'manifest_inventory_root_override': {'original': str(manifest_root), 'selected': str(package_root)},
                 'kokoro_directory': {'package_default': str(original_tts_directory),
                                      'selected': str(local_tts.MODELS_DIR), 'reference_sha256': tts_references},
                 'limits': ['Loaded module paths/hashes are runtime import provenance, not inference proof.',
                            'Recorder client hashes the current root agent tree; baseline worker intentionally imports its separately frozen package.',
                            'Diagnostics remain buffered until worker exit; forced kill can leave them unavailable.']}
    directory.mkdir(parents=True, exist_ok=True)
    save(selection_path, selection, exclusive=True)
    diagnostics = observer.STTDiagnostics(get_job_context)
    status = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'starting'}
    original_argv = sys.argv[:]
    try:
        diagnostics.install(local_stt, parakeet)
        sys.argv = [original_argv[0], '--manifest', str(args.manifest)]
        launcher.main()
        status['status'] = 'launcher_returned'
    except BaseException as error:
        status.update(status='launcher_exited', exception=repr(error))
        raise
    finally:
        sys.argv = original_argv
        local_livekit_worker.ROOT = manifest_root
        diagnostics.restore()
        saved = diagnostics.flush(directory / 'stt-diagnostics')
        status.update(finished_at_utc=datetime.now(timezone.utc).isoformat(), diagnostics_saved=saved,
                      diagnostic_error=diagnostics.flush_error, kokoro_directory_at_exit=str(local_tts.MODELS_DIR),
                      kokoro_directory_unchanged=local_tts.MODELS_DIR == ROOT / 'models/kokoro')
        loaded_after, errors_after = loaded_agent_modules(package_root, frozen)
        status.update(loaded_agent_modules=loaded_after, module_provenance_errors=errors_after,
                      initial_modules_unchanged=all(loaded_after.get(name) == row for name, row in loaded.items()))
        save(directory / 'stt-diagnostics-exit.json', status, exclusive=True)
        save(exit_path, {**status, 'policy': args.policy, 'agent_package_root': str(package_root)}, exclusive=True)
        if not saved or errors_after or not status['initial_modules_unchanged'] or not status['kokoro_directory_unchanged']:
            print('Worker diagnostic/provenance validation failed; see retained exit receipt', file=sys.stderr)
            raise RuntimeError('Worker diagnostic/provenance validation failed')


if __name__ == '__main__':
    main()
