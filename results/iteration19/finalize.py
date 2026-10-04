"""Verify the completed development checkpoint without declaring the goal done."""
import ast
import json
from pathlib import Path
import subprocess
from common import BASE, ROOT, absent, digest, save, stamp, verify_frozen


def main():
    target = BASE / 'final-validation.json'
    report = dict(status='running', started_at_utc=stamp(), task_complete=False,
                  checks={}, issues=[], artifact_sha256={})
    save(target, report, exclusive=True)

    def read(path):
        return json.loads(Path(path).read_text())

    def check(name, value):
        report['checks'][name] = bool(value)
        if not value:
            report['issues'].append(name)

    try:
        frozen = verify_frozen()
        post = read(BASE / 'post-inference-validation.json')
        check('all_post_phases_completed', post['status'] == 'passed'
              and post['all_phases_reached'] and not post['issues'])
        snapshots = read(BASE / 'source-snapshot.json')['files']
        assets = read(BASE / 'input-assets.json')['assets']
        maps = dict(source_files=frozen['source_sha256'],
                    snapshot_files={r['snapshot']: r['sha256'] for r in snapshots},
                    model_files={p: r['reference'] for p, r in frozen['model_files'].items()},
                    input_files={p: r['sha256'] for p, r in assets.items()})
        check('fixed_inventory_sizes', [len(maps[k]) for k in maps] == [154, 154, 10, 8])
        for kind, expected in maps.items():
            observed = post[kind]
            check('post_inventory:' + kind, set(observed) == set(expected))
            check('stable_post_bytes:' + kind, all(r['matches'] and r['stable_stat_during_read']
                  and r['expected'] == r['observed'] == expected[p]
                  and r['bytes_read'] == r['stat_before']['st_size']
                  and r['stat_before'] == r['stat_after'] == r['path_stat_after']
                  for p, r in observed.items()))
        check('snapshot_census', {r['original']: r['sha256'] for r in snapshots} == frozen['source_sha256'])
        check('snapshot_receipt', digest(BASE / 'source-snapshot.json') == frozen['snapshot_sha256'])
        check('snapshot_bytes', all(digest(p) == h for p, h in maps['snapshot_files'].items()))
        check('input_bytes', all(digest(BASE / 'audio-assets' / p) == h for p, h in maps['input_files'].items()))
        phases = ('inputs', 'baseline', 'candidate', 'asr-baseline', 'asr-candidate')
        check('phase_inventory', set(post['phases']) == set(phases))
        expected_pids = []
        for name in phases:
            phase = read(BASE / f'{name}-supervision.json')
            check('phase:' + name, post['phases'][name] == phase and phase['status'] == 'exited'
                  and phase['exit_code'] == 0 and phase['finished_at_utc'] > phase['started_at_utc']
                  and not phase.get('deadline_exceeded') and not phase.get('forced_kill'))
            expected_pids.append(dict(role=name, pid=phase['pid']))
        protocol = read(BASE / 'protocol.json')
        keys = [(arm['id'], case) for arm in protocol['arms'] for case in arm['cases']]
        for arm in protocol['arms']:
            directory = ROOT / 'results' / f"iteration19-{arm['id']}"
            suite = read(directory / 'run-report.json')
            check('suite:' + arm['id'], suite['status'] == 'completed' and suite['all_agents_departed']
                  and suite['service_cleanup_complete'] and suite['frozen_inputs_unchanged']
                  and [r['case'] for r in suite['runs']] == arm['cases'])
            for row in suite['cleanup']:
                launch = read(directory / 'local-stack-run1' / f"{row['role']}-launch.json")
                check(f"cleanup:{arm['id']}/{row['role']}", row['pid'] == launch['pid']
                      and row['exit_code'] == (-15 if row['role'] == 'llm' else 0)
                      and not row.get('cleanup_error') and not row.get('escalation'))
                expected_pids.append(dict(role=f"{arm['id']}/{row['role']}", pid=row['pid']))
            expected_pids.extend(dict(role=f"{arm['id']}/{r['name']}", pid=r['pid']) for r in suite['runs'])
            audio = read(directory / 'received-audio-review/report.json')
            check('received_asr:' + arm['id'], audio['status'] == 'completed'
                  and [r['case'] for r in audio['trials']] == arm['cases']
                  and all(r['status'] == 'completed' for r in audio['trials']))
        check('exact_pid_census', post['pids'] == expected_pids and len(expected_pids) == 21
              and len({r['pid'] for r in expected_pids}) == 21)
        for field in ('pids_before_models', 'pids_after_models'):
            check(field, post[field] == [dict(r, absent=True) for r in expected_pids])
        recovery = read(BASE / 'candidate-setup-recovery.json')
        archived = ROOT / 'results/iteration19-candidate/startup-attempt1'
        failed = read(archived / 'run-report.json')
        failed_phase = read(archived / 'candidate-supervision.json')
        check('setup_failure_preserved', failed['status'] == 'failed' and failed['runs'] == failed['cleanup'] == []
              and failed_phase['status'] == 'exited' and failed_phase['exit_code'] == 1
              and failed_phase['pid'] == recovery['failed_pid'] and len(recovery['files']) == 4
              and all(digest(r['archived']) == r['sha256'] for r in recovery['files'].values()))
        check('retry_unchanged_command', failed_phase['command'] == post['phases']['candidate']['command'])
        expected_pids.append(dict(role='candidate/failed_setup_attempt1', pid=recovery['failed_pid']))
        report['fresh_pid_observations'] = [dict(r, absent=absent(r['pid'])) for r in expected_pids]
        check('all_22_recorded_pids_absent', len({r['pid'] for r in expected_pids}) == 22
              and all(r['absent'] for r in report['fresh_pid_observations']))
        tests = read(BASE / 'tested-source-v2.json')
        check('tested_sources', tests['exit_code'] == 0 and tests['unchanged_during_test']
              and len(tests['paths_sha256']) == 85
              and all(digest(ROOT / p) == h for p, h in tests['paths_sha256'].items()))
        check('test_log', digest(BASE / 'full-tests-v2.txt') == tests['log_sha256']
              and '755 passed in 5.78s' in (BASE / 'full-tests-v2.txt').read_text())
        audit = read(BASE / 'acoustic-audit.json')
        speech = read(BASE / 'speech-fact-review.json')
        latency = read(BASE / 'latency-review.json')
        check('audit_cases', [(a['arm_id'], c['case_id']) for a in audit['arms'] for c in a['cases']] == keys)
        check('speech_cases', [(c['arm'], c['case']) for c in speech['cases']] == keys)
        check('latency_cases', [(c['arm'], c['case_id']) for c in latency['per_case']] == keys)
        check('audit_results', not audit['evidence_issues'] and audit['summary']['declared_captures'] == 10
              and audit['summary']['declared_expected_mutations'] == 6
              and audit['summary']['observed_successful_mutations'] == 1
              and audit['summary']['physical_attempts'] == 22)
        check('speech_results', speech['summary']['all']['final_required_facts_pass'] == 3
              and speech['summary']['all']['initial_required_clarifications_pass'] == 6
              and speech['summary']['baseline']['final_required_facts_pass'] == 1
              and speech['summary']['candidate']['final_required_facts_pass'] == 2)
        check('latency_failures_retained', not latency['evidence_issues']
              and latency['summary']['declared_cases'] == 10 and latency['summary']['fulfilled_finish_measured'] == 3)
        counts = audit['summary']['criteria_counts']
        check('capture_and_trace_results', counts['capture_completed']['passed'] == 10
              and counts['raw_trace_equality']['passed'] == 10
              and counts['trusted_trace_equality']['passed'] == 6
              and counts['trusted_trace_equality']['failed'] == 4)
        reviews = [BASE / n for n in ('production-review.json', 'auditor-final-review-v2.json',
                   'acoustic-audit.json', 'speech-fact-review.json', 'latency-review.json', 'docs-review.json')]
        reviews += [ROOT / 'results' / f'iteration19-{a}' / f'{a}-mechanism-review.json'
                    for a in ('baseline', 'candidate')]
        for path in reviews:
            review = read(path)
            check('review_issues:' + path.name, all(not review.get(k) for k in
                  ('issues', 'evidence_issues', 'open_findings', 'open_concrete_findings')))
            for field in ('evidence_sha256', 'source_sha256', 'reviewed_sha256'):
                for name, expected in review.get(field, {}).items():
                    source = Path(name) if Path(name).is_absolute() else ROOT / name
                    check(f'review_reference:{path.name}:{name}', source.is_file() and digest(source) == expected)
        documentation = read(BASE / 'docs-review.json')
        check('docs_passed', documentation['status'] == 'passed')
        git = subprocess.run(['git', 'diff', '--check'], cwd=ROOT, capture_output=True, text=True)
        report['whitespace_check'] = dict(exit_code=git.returncode, stdout=git.stdout, stderr=git.stderr)
        check('whitespace', git.returncode == 0)
        report['pr_status'] = read(BASE / 'pr-status.json')
        report['known_failures'] = ['Baseline hospital lookup-only reply',
            'Both arms misrecognize Hebbal and Indiranagar; four unknown-place final failures',
            'Both distance questions omit required route computation', 'Four trusted-trace equality failures',
            'Candidate first setup fails before services/captures; one explicitly disclosed setup retry',
            'Substantial shared-host pressure and slower observed candidate replies; no latency benefit demonstrated']
        report['decision'] = 'Retain grounded continuation with one supported live replay and component boundaries; preserve all acoustic failures.'
        report['goal_remaining'] = ['Distance-query planning', 'Independent place-name recognition experiments',
            'Concurrent RTC rooms', 'Acoustic policy/acknowledgment comparisons',
            'Human microphone/echo/physical playback', 'Organizer and fresh adapted FDB evidence',
            'User-selected staging, commit and PR publication']
        for path in BASE.glob('*.py'):
            ast.parse(path.read_text(), filename=str(path))
        paths = set()
        for directory in (BASE, ROOT / 'results/iteration19-baseline', ROOT / 'results/iteration19-candidate'):
            paths.update(p for p in directory.rglob('*') if p.is_file() and '__pycache__' not in p.parts
                         and p.suffix != '.pyc' and p != target and p.name != 'final-validation-output.txt')
        paths.update(ROOT / p for p in ('README.md', 'docs/iteration19.md', 'docs/completion-audit.md'))
        paths.add(ROOT.parent / 'prism-pr-description.md')
        report['artifact_sha256'] = {str(p): digest(p) for p in sorted(paths)}
        check('artifact_inventory_stable', all(digest(p) == h for p, h in report['artifact_sha256'].items()))
        report['status'] = 'checkpoint_verified_with_known_failures' if not report['issues'] else 'failed'
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        report['finished_at_utc'] = stamp()
        save(target, report)
    print(json.dumps(dict(status=report['status'], issues=report['issues'],
          artifacts=len(report['artifact_sha256']), checks=len(report['checks']), task_complete=False)))
    return int(report['status'] == 'failed')


if __name__ == '__main__':
    raise SystemExit(main())
