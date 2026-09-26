"""Close the partial development checkpoint; never imply full protocol completion."""
import json
from pathlib import Path
import subprocess
from common import BASE, ROOT, absent, digest, save, stamp, verify_frozen


def main():
    target = BASE / 'final-validation.json'
    report = dict(status='running', started_at_utc=stamp(), task_complete=False,
                  full_declared_protocol_completed=False, checks={}, issues=[], artifact_sha256={})
    save(target, report, exclusive=True)
    def read(p):
        return json.loads(Path(p).read_text())
    def check(name, condition):
        report['checks'][name] = bool(condition)
        if not condition:
            report['issues'].append(name)
    try:
        frozen = verify_frozen()
        post = read(BASE / 'post-inference-validation-release.json')
        check('partial_post_verified', post['status']=='passed' and not post['issues']
              and post['all_phases_reached'] and post['full_declared_protocol_completed'] is False)
        check('post_inventory_sizes', [len(post[k]) for k in ('source_files','snapshot_files','model_files','input_files')]==[157,157,10,7])
        for kind in ('source_files','snapshot_files','model_files','input_files'):
            check('stable_bytes:'+kind, all(r['matches'] and r['stable_stat_during_read']
                  and r['observed']==r['expected'] and r['stat_before']==r['stat_after']==r['path_stat_after']
                  and r['bytes_read']==r['stat_before']['st_size'] for r in post[kind].values()))
        snapshots = read(BASE / 'source-snapshot.json')['files']
        check('snapshot_census', {r['original']:r['sha256'] for r in snapshots}==frozen['source_sha256'])
        check('snapshot_bytes', all(digest(r['snapshot'])==r['sha256'] for r in snapshots))
        check('input_bytes', all(digest(BASE/'audio-assets'/p)==r['expected'] for p,r in post['input_files'].items()))
        expected_exits={'inputs':0,'baseline':1,'candidate':1,'asr-baseline':1,'asr-candidate':0}
        check('phase_inventory', set(post['phases'])==set(expected_exits))
        for name,code in expected_exits.items():
            row=post['phases'][name]
            check('phase:'+name,row['status']=='exited' and row['exit_code']==code
                  and not row.get('deadline_exceeded') and not row.get('forced_kill'))
        report['fresh_pid_observations']=[dict(r,absent=absent(r['pid'])) for r in post['pids']]
        check('owned_pids_absent',len(post['pids'])==len({r['pid'] for r in post['pids']})==19
              and all(r['absent'] for r in report['fresh_pid_observations']))
        tests=read(BASE/'tested-source-v1.json')
        check('tested_sources',tests['exit_code']==0 and tests['unchanged_during_test']
              and len(tests['paths_sha256'])==86
              and all(digest(ROOT/p)==h for p,h in tests['paths_sha256'].items()))
        check('test_log',digest(BASE/'full-tests-v1.txt')==tests['log_sha256']
              and '797 passed in 6.02s' in (BASE/'full-tests-v1.txt').read_text())
        frontend=read(BASE/'frontend-release-validation.json')
        check('frontend',frontend['status']=='passed' and frontend['unchanged']
              and all(digest(ROOT/p)==h for p,h in frontend['source_sha256'].items())
              and all(r['exit_code']==0 and digest(BASE/(r['name']+'-release.txt'))==r['log_sha256'] for r in frontend['checks']))
        protocol=read(BASE/'protocol.json')
        keys=[(a['id'],c) for a in protocol['arms'] for c in a['cases']]
        audit=read(BASE/'acoustic-audit.json');speech=read(BASE/'speech-fact-review.json');latency=read(BASE/'latency-review.json')
        check('audit_inventory',[(a['arm_id'],c['case_id']) for a in audit['arms'] for c in a['cases']]==keys)
        check('speech_inventory',[(c['arm'],c['case']) for c in speech['cases']]==keys)
        check('latency_inventory',[(c['arm'],c['case_id']) for c in latency['per_case']]==keys)
        check('missing_baseline_preserved',audit['summary']['case_status_counts']=={'extracted':7,'missing_capture_report':1,'unrun':4})
        check('expected_evidence_gaps',len(audit['evidence_issues'])==5 and all('iteration20-baseline/rtc-' in r['path'] and r['error']=='missing_file' for r in audit['evidence_issues']))
        check('latency_issues_absent',not latency['evidence_issues'])
        for filename in ('acoustic-audit.json','speech-fact-review.json','latency-review.json','harness-amendment.json','mechanism-review.json','docs-review.json'):
            obj=read(BASE/filename)
            for field in ('evidence_sha256','source_sha256','new_method_sha256','preserved_evidence_sha256','documents_sha256','document_sha256'):
                for name,h in obj.get(field,{}).items():
                    p=Path(name) if Path(name).is_absolute() else ROOT/name
                    check('reference:'+filename+':'+name,p.is_file() and digest(p)==h)
        check('docs_review',read(BASE/'docs-review.json')['status']=='passed')
        whitespace=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True)
        check('whitespace',whitespace.returncode==0)
        report['observed_results']={'acoustic':audit['summary'],'speech':speech['summary'],'latency':latency['summary']}
        report['decision']='Retain the tested development checkpoint with explicit acoustic failures and partial baseline; no production-readiness or full-protocol claim.'
        report['pr_status']=read(BASE/'pr-status.json')
        paths=set()
        for directory in (BASE,ROOT/'results/iteration20-baseline',ROOT/'results/iteration20-candidate'):
            paths.update(p for p in directory.rglob('*') if p.is_file() and '__pycache__' not in p.parts
                         and p.suffix!='.pyc' and p!=target and p.name!='final-validation-output.txt')
        paths.update(ROOT/p for p in ('README.md','docs/iteration20.md','docs/completion-audit.md'))
        paths.add(ROOT.parent/'prism-pr-description.md')
        report['artifact_sha256']={str(p):digest(p) for p in sorted(paths)}
        check('artifact_bytes',all(digest(p)==h for p,h in report['artifact_sha256'].items()))
        report['status']='checkpoint_verified_with_known_failures' if not report['issues'] else 'failed'
    except BaseException as error:
        report.update(status='failed',error=repr(error));raise
    finally:
        report['finished_at_utc']=stamp();save(target,report)
    print(json.dumps({k:report[k] for k in ('status','issues','task_complete','full_declared_protocol_completed')}))
    return int(bool(report['issues']))

if __name__=='__main__':
    raise SystemExit(main())
