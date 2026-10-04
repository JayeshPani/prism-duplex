"""Record final evidence links after all iteration10 work is terminal."""
from datetime import datetime, timezone
from hashlib import sha256
import ast
import json
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent

def read(name):
    return json.loads((BASE / name).read_text())

def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()

number = read('number-analysis.json')
audit = read('audit-v2.json')
latency = read('latency-summary-v2.json')
speech = read('received-audio-review/qualitative-review.json')
logs = read('supplementary-log-review.json')
checks = read('final-checks.json')
post = read('post-inference-validation.json')
for script in BASE.glob('*.py'):
    ast.parse(script.read_text(), filename=str(script))
assert all(row['matches'] for row in post['model_hashes'].values())
assert post['review_pid_absent'] and read('pre-asr-cleanup.json')['all_owned_processes_absent']
assert not audit['current_frozen_input_mismatches']
assert git('diff', '--check') == ''
paths = [p for p in BASE.rglob('*') if p.is_file() and '__pycache__' not in p.parts
         and p.name != 'final-validation.json']
paths += [ROOT / 'README.md', ROOT / 'docs/iteration10.md', ROOT.parent / 'prism-pr-description.md']
report = {
    'checked_at_utc': datetime.now(timezone.utc).isoformat(),
    'production_change': 'Retain two stop-summary strings using is before duration; speed/config unchanged.',
    'tests': {'passed': 543, 'seconds': 5.44, 'evidence': 'full-suite-candidate.txt',
              'source_unchanged_since_tests': checks['number_protocol_sources_still_match']},
    'offline_speech': {'counts': number['counts'], 'selection': number['selection']},
    'rtc': {'status': read('run-report.json')['status'], 'summary': audit['summary'],
            'failed_checks': audit['failed_checks'], 'undeclared_runs': audit['undeclared_runs'],
            'expected_attempts': 19, 'expected_effects': 7,
            'interruption_rule': 'All three original result handles finish; no successful original-result interruption observed.',
            'later_intent': 'Waypoint2 final effect/result belongs to intent3. Frozen intent2 checks stay failed.',
            'content': 'Both waypoint streams retain13/zeroextra, baseline airport88, failed correction onlyairport88. No office29/replacement.',
            'analysis_correction': 'Original audit and latency preserved; v2 requires interruption_requested before labeling an interruption time.'},
    'latency_summary': 'latency-summary-v2.json',
    'supplementary_log_review': 'supplementary-log-review.json',
    'received_content_review': 'received-audio-review/qualitative-review.json',
    'source_and_model_checks_pass': True,
    'all_owned_processes_absent_after_inference': True,
    'git': {'branch': git('branch', '--show-current'), 'head': git('rev-parse', 'HEAD'),
            'origin_main': git('rev-parse', 'origin/main'),
            'staged_files': git('diff', '--cached', '--name-only').splitlines(),
            'commits_ahead': git('log', '--format=%H', 'origin/main..HEAD').splitlines(),
            'delivery': 'No PR opened: staging remains reserved to the user by workspace instructions.'},
    'artifacts_sha256': {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p): sha256(p.read_bytes()).hexdigest()
                        for p in sorted(paths)},
    'remaining': ['User-selected staging for commit/PR', 'Intermittent missing/slow recognition and ineffective interruption',
                  'Synchronous trace-file I/O evidenced by436ms loop stall', 'Server failed-job status ordering',
                  'Organizer Theme05 brief access', 'Human microphone/echo and physical playback',
                  'Live cancellation/repeat visits, concurrent rooms, fresh adaptedFDB regression'],
}
with (BASE / 'final-validation.json').open('x') as out:
    out.write(json.dumps(report, indent=2) + '\n')
print(json.dumps({'tests': report['tests'], 'rtc_status': report['rtc']['status'],
                  'attempts': audit['summary']['shared_attempts'], 'effects': audit['summary']['observed_effects'],
                  'git': report['git'], 'artifact_count': len(paths)}))
