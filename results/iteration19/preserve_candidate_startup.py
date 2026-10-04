"""Retain the failed pre-service startup before one disclosed setup retry."""
import json
from pathlib import Path
import socket
from common import BASE, ROOT, absent, digest, save, stamp, verify_frozen

arm = ROOT / 'results/iteration19-candidate'
suite = json.loads((arm / 'run-report.json').read_text())
phase = json.loads((BASE / 'candidate-supervision.json').read_text())
assert suite['status'] == 'failed' and not suite['runs'] and not suite['cleanup']
assert suite['error'] == "OSError(48, 'Address already in use')"
assert phase['status'] == 'exited' and phase['exit_code'] == 1 and absent(phase['pid'])
assert sorted(p.name for p in (arm / 'local-stack-run1').iterdir()) == ['conditions', 'livekit.yaml']
assert not list((arm / 'local-stack-run1/conditions').iterdir())
verify_frozen()
ports = []
for port, kind in ((7880, socket.SOCK_STREAM), (7882, socket.SOCK_DGRAM),
                   (8081, socket.SOCK_STREAM), (8082, socket.SOCK_STREAM)):
    with socket.socket(socket.AF_INET, kind) as stream:
        stream.bind(('127.0.0.1', port))
    ports.append(dict(port=port, kind=int(kind), bind_available=True))
archive = arm / 'startup-attempt1'
archive.mkdir(exist_ok=False)
paths = [arm / 'run-report.json', arm / 'local-stack-run1',
         BASE / 'candidate-supervision.json', BASE / 'candidate-controller.log']
files = [f for p in paths for f in ([p] if p.is_file() else p.rglob('*')) if f.is_file()]
mapping = {str(f): dict(sha256=digest(f), archived=str(archive / (
    f.relative_to(arm) if f.is_relative_to(arm) else Path(f.name)))) for f in files}
receipt = dict(status='preserving', created_at_utc=stamp(), failed_pid=phase['pid'],
    ports_available_before_retry=ports, files=mapping,
    method_sha256=digest(__file__),
    deviation='One post-freeze setup retry after a pre-service bind failure. Original no-retry protocol is superseded only for this setup attempt; no service, input publication or capture ran in it. All five candidate conversations will still be first attempts. No outcome-selected reruns or source/model/configuration changes.',
    diagnosis='No listener found by lsof after the failure; all four binds now succeed. Transient socket reuse is plausible but the failed port and kernel state were not recorded, so the cause is not proven.')
save(BASE / 'candidate-setup-recovery.json', receipt, exclusive=True)
for path in paths:
    path.rename(archive / path.name)
assert all(digest(row['archived']) == row['sha256'] for row in mapping.values())
receipt.update(status='preserved_retry_authorized_by_declared_amendment', preserved_at_utc=stamp())
save(BASE / 'candidate-setup-recovery.json', receipt)
print(json.dumps(dict(status=receipt['status'], preserved_files=len(files), failed_pid=phase['pid'])))
