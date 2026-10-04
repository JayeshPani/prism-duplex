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
command = [sys.executable, str(BASE / 'review_received_audio.py'), '--suite-root', str(BASE), '--out', str(BASE / 'received-audio-review')]
# Keep all received-speech inference outside every timed arm.
for arm_name in ("iteration17-cache2-before", "iteration17-cache1", "iteration17-cache2-after"):
    suite = json.loads((ROOT / "results" / arm_name / "run-report.json").read_text())
    assert suite.get("finished_at") and suite.get("status") not in {"starting", "running"}, arm_name
    pids = {row["pid"] for row in suite["runs"]} | {row["pid"] for row in suite["cleanup"]}
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise RuntimeError(f"Timed arm process still exists: {arm_name}/{pid}")
path = BASE / 'received-audio-supervision.json'
receipt = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'command': command,
           'status': 'starting', 'deadline_seconds': 300, 'sigterm_grace_seconds': 20}
with path.open('x') as stream:
    json.dump(receipt, stream, indent=2)

def save():
    path.write_text(json.dumps(receipt, indent=2) + '\n')

with (BASE / 'received-audio-review.log').open('x') as log:
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
