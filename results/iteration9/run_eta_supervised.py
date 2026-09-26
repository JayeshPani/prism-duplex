"""Own the offline speech probe and retain native-timeout/exit evidence."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent
command = [sys.executable, str(BASE / 'measure_eta_speech.py'), '--protocol',
           str(BASE / 'eta-protocol.json'), '--out', str(BASE / 'eta-speech')]
path = BASE / 'eta-supervision.json'
receipt = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'command': command,
           'status': 'starting', 'deadline_seconds': 300, 'sigterm_grace_seconds': 20}
with path.open('x') as stream:
    json.dump(receipt, stream, indent=2)

def save():
    path.write_text(json.dumps(receipt, indent=2) + '\n')

with (BASE / 'eta-speech.log').open('x') as log:
    process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    receipt.update(status='running', pid=process.pid)
    save()
    try:
        process.wait(timeout=300)
    except subprocess.TimeoutExpired:
        receipt['deadline_exceeded'] = True
    finally:
        if process.poll() is None:
            receipt['sigterm_sent'] = True
            save()
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                receipt['forced_kill'] = True
                save()
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        receipt.update(status='exited', exit_code=process.returncode,
                       finished_at_utc=datetime.now(timezone.utc).isoformat())
        save()
print(json.dumps(receipt), flush=True)
raise SystemExit(process.returncode or bool(receipt.get('deadline_exceeded')))
