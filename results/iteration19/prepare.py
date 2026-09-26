"""Materialize the isolated pre-continuation package after production edits settle.

Model-free preparation only; run once before the parent-owned source freeze.
"""
import json
import shutil
from common import BASE, ROOT, digest, save, stamp


def main():
    destination = BASE / 'baseline-package'
    receipt_path = BASE / 'source-preparation.json'
    assert not destination.exists() and not receipt_path.exists(), 'Preserve prior preparation; no overwrite'
    assert not (BASE / 'preflight.json').exists(), 'Prepare before source freeze'
    baseline = json.loads((BASE / 'baseline.json').read_text())
    candidates = sorted(p for p in (ROOT / 'agent').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    candidate_hashes = {str(p.relative_to(ROOT)): digest(p) for p in candidates}
    overrides = baseline['baseline_sources']
    assert len(overrides) == 5
    assert all(digest(BASE / 'baseline-source' / name) == value for name, value in overrides.items())
    receipt = {'started_at_utc': stamp(), 'status': 'preparing', 'candidate_source_sha256': candidate_hashes,
               'baseline_overrides_sha256': overrides, 'baseline_source_receipt_sha256': digest(BASE / 'baseline.json')}
    save(receipt_path, receipt, exclusive=True)
    try:
        destination.mkdir()
        for name in candidate_hashes:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        for name in overrides:
            shutil.copyfile(BASE / 'baseline-source' / name, destination / name)
        expected = {**candidate_hashes, **overrides}
        observed = {name: digest(destination / name) for name in expected}
        assert observed == expected, 'Baseline copy/overlay mismatch'
        assert {str(p.relative_to(ROOT)): digest(p) for p in candidates} == candidate_hashes, 'Candidate changed during preparation'
        assert set(candidate_hashes) == {str(p.relative_to(ROOT)) for p in (ROOT / 'agent').rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}, 'Candidate inventory changed'
        receipt.update(status='prepared', baseline_package_root=str(destination), baseline_package_sha256=observed,
                       unchanged_during_preparation=True,
                       limits='No test/model/service run. Both packages retain the iteration18 recognition-aware output; five historical files alone define the continuation control.')
    except BaseException as error:
        receipt.update(status='failed', error=repr(error))
        raise
    finally:
        receipt['finished_at_utc'] = stamp()
        save(receipt_path, receipt)
    print(json.dumps({'status': receipt['status'], 'package_files': len(observed), 'overrides': list(overrides)}, indent=2))


if __name__ == '__main__':
    main()
