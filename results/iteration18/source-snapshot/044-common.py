"""Shared bounded experiment bookkeeping; no model imports."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import os

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value, exclusive=False):
    path = Path(path)
    if exclusive:
        with path.open('x') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
    else:
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(value, indent=2) + '\n')
        temporary.replace(path)


def verify_frozen():
    frozen = json.loads((BASE / 'preflight.json').read_text())
    assert frozen['status'] == 'passed'
    bad = [name for name, expected in frozen['source_sha256'].items() if digest(name) != expected]
    if bad:
        raise RuntimeError(f'Frozen source changed: {bad}')
    return frozen


def absent(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False
