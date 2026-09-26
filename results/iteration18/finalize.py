"""Finalize a verified checkpoint with explicit failures; never declare goal completion."""
import ast
import json
from pathlib import Path
import subprocess
from common import BASE, ROOT, absent, digest, save, stamp, verify_frozen


def main():
    output=BASE/'final-validation.json'
    report=dict(status='running',started_at_utc=stamp(),task_complete=False,issues=[],checks={},artifact_sha256={})
    save(output,report,exclusive=True)
    def check(name,condition):
        report['checks'][name]=bool(condition)
        if not condition:report['issues'].append(name)
    def read(path):return json.loads(Path(path).read_text())
    try:
        preflight=verify_frozen()
        post=read(BASE/'post-inference-validation.json')
        check('post_all_phases',post['status']=='passed' and post['all_phases_reached'] and not post['issues'])
        check('post_inventory',len(post['model_files'])==10 and len(post['source_files'])==len(post['snapshot_files'])==117 and len(post['input_files'])==13 and len(post['pids'])==32)
        check('post_stable_bytes',all(r['matches'] and r['stable_stat_during_read'] for k in ('model_files','source_files','snapshot_files','input_files') for r in post[k].values()))
        report['fresh_pid_observations']=[dict(r,absent=absent(r['pid'])) for r in post['pids']]
        check('recorded_processes_absent',all(r['absent'] for r in report['fresh_pid_observations']))
        snapshots=read(BASE/'source-snapshot.json')
        assets=read(BASE/'input-assets.json')
        expected_maps={
            'source_files':preflight['source_sha256'],
            'model_files':{p:r['reference'] for p,r in preflight['model_files'].items()},
            'snapshot_files':{r['snapshot']:r['sha256'] for r in snapshots['files']},
            'input_files':{p:r['sha256'] for p,r in assets['assets'].items()},
        }
        for category,expected in expected_maps.items():
            rows=post[category]
            check(f'post_exact_inventory:{category}',set(rows)==set(expected) and all(
                r['expected']==r['observed']==expected[p] and
                r['bytes_read']==r['stat_before']['st_size'] and
                r['stat_before']==r['stat_after']==r['path_stat_after']
                for p,r in rows.items()))
        phases={'inputs':0,'baseline':1,'candidate':0,'asr-baseline':0,'asr-candidate':0}
        check('post_exact_phases',set(post['phases'])==set(phases))
        expected_pids=[]
        for name,exit_code in phases.items():
            phase=read(BASE/f'{name}-supervision.json')
            check(f'post_phase:{name}',post['phases'][name]==phase and phase['status']=='exited'
                  and bool(phase['started_at_utc']) and phase['finished_at_utc']>phase['started_at_utc']
                  and phase['exit_code']==exit_code)
            expected_pids.append(dict(role=name,pid=phase['pid']))
        protocol=read(BASE/'protocol.json')
        for arm in protocol['arms']:
            suite=read(ROOT/'results'/f"iteration18-{arm['id']}"/'run-report.json')
            check(f"suite_cases:{arm['id']}",[r['case'] for r in suite['runs']]==arm['cases'])
            for row in suite['cleanup']:
                launch=read(ROOT/'results'/f"iteration18-{arm['id']}"/'local-stack-run1'/f"{row['role']}-launch.json")
                check(f"cleanup:{arm['id']}/{row['role']}",row['pid']==launch['pid'] and row['exit_code']==(-15 if row['role']=='llm' else 0))
                expected_pids.append(dict(role=f"{arm['id']}/{row['role']}",pid=row['pid']))
            expected_pids.extend(dict(role=f"{arm['id']}/{r['name']}",pid=r['pid']) for r in suite['runs'])
        check('post_exact_pids',post['pids']==expected_pids and len(expected_pids)==len({r['pid'] for r in expected_pids})==32)
        for field in ('pids_before_models','pids_after_models'):
            check(field,post[field]==[dict(r,absent=True) for r in expected_pids])
        check('snapshot_receipt',digest(BASE/'source-snapshot.json')==preflight['snapshot_sha256'])
        check('snapshot_census',len(snapshots['files'])==117 and {r['original']:r['sha256'] for r in snapshots['files']}==preflight['source_sha256'])
        check('snapshot_bytes',all(digest(r['snapshot'])==r['sha256'] for r in snapshots['files']))
        tested=read(BASE/'tested-source-v2.json')
        check('tested_sources',tested['exit_code']==0 and tested['unchanged_during_test'] and len(tested['paths_sha256'])==79 and all(digest(ROOT/p)==h for p,h in tested['paths_sha256'].items()))
        check('test_output',digest(BASE/'full-tests-v2.txt')==tested['log_sha256'] and '696 passed in 5.87s' in (BASE/'full-tests-v2.txt').read_text())
        audit=read(BASE/'acoustic-audit.json');speech=read(BASE/'speech-fact-review.json');latency=read(BASE/'latency-review.json')
        case_keys=[(a['id'],c) for a in protocol['arms'] for c in a['cases']]
        check('acoustic_case_inventory',[(a['arm_id'],c['case_id']) for a in audit['arms'] for c in a['cases']]==case_keys)
        check('speech_case_inventory',[(c['arm'],c['case']) for c in speech['cases']]==case_keys)
        check('latency_case_inventory',[(c['arm'],c['case_id']) for c in latency['per_case']]==case_keys)
        declared={c['id']:c for c in protocol['cases']}
        check('declared_mutations',sum(len(declared[c]['expected']['effects']) for _,c in case_keys)==audit['summary']['declared_expected_mutations']==25)
        check('auditor_full_inventory',not audit['evidence_issues'] and audit['summary']['declared_captures']==21 and audit['summary']['observed_successful_mutations']==23 and audit['summary']['physical_attempts']==63)
        check('speech_denominator',speech['summary']['combined']['declared_cases']==21 and speech['summary']['combined']['ASR_completed']==21 and speech['summary']['combined']['final_required_facts_present']==19)
        check('latency_failures_retained',not latency['evidence_issues'] and latency['summary']['declared_cases']==21 and latency['summary']['fulfilled_finish_measured']==19)
        counts=audit['summary']['criteria_counts']
        check('quality_failures_preserved',counts['capture_completed']['failed']==1 and counts['effect_count']['failed']==2 and counts['trusted_trace_equality']['failed']==11 and counts['voiced_stop_received_cutoff_screen']['failed']==1)
        check('raw_trace_complete',counts['raw_trace_equality']['passed']==21)
        reviews=[BASE/'acoustic-audit.json',BASE/'speech-fact-review.json',BASE/'latency-review.json',
                 BASE/'combined-mechanism-comparison.json',BASE/'experimental-harness-review.json',BASE/'freeze-review.json',BASE/'docs-review.json',
                 ROOT/'results/iteration18-baseline/baseline-mechanism-review.json',ROOT/'results/iteration18-candidate/candidate-mechanism-review.json']
        review_contracts={
            'acoustic-audit.json':('extracted_manual_speech_review_pending',209),
            'speech-fact-review.json':('review_completed_failures_preserved',139),
            'latency-review.json':('completed',25),
            'combined-mechanism-comparison.json':('complete_with_failures_preserved',20),
            'experimental-harness-review.json':('reviewed_no_open_concrete_blocker',27),
            'freeze-review.json':('passed',8),
            'docs-review.json':('passed',21),
            'baseline-mechanism-review.json':('completed_baseline_only_read_only_review',61),
            'candidate-mechanism-review.json':('completed_candidate_only_read_only_review',91),
        }
        checked={}
        for path in reviews:
            r=read(path)
            mapping={}
            for field in ('evidence_sha256','source_sha256','reviewed_sha256'):
                for name,value in r.get(field,{}).items():
                    check(f'review_consistent_hash:{path.name}:{name}',name not in mapping or mapping[name]==value)
                    mapping[name]=value
            if path.name=='docs-review.json':
                expected_docs={str(ROOT/p) for p in ('README.md','docs/iteration18.md','docs/completion-audit.md')}
                expected_docs.add(str(ROOT.parent/'prism-pr-description.md'))
                check('reviewed_document_inventory',set(r['reviewed_sha256'])==expected_docs)
            status,minimum=review_contracts[path.name]
            check(f'review_status:{path.name}',r['status']==status)
            check(f'review_coverage:{path.name}',len(mapping)>=minimum and all(isinstance(v,str) and len(v)==64 for v in mapping.values()))
            check(f'review_issues:{path.name}',all(not r.get(k) for k in ('issues','evidence_issues','open_findings')))
            check(f'review_checks:{path.name}',all(v is True for v in r.get('checks',{}).values() if isinstance(v,bool)))
            if path.name=='combined-mechanism-comparison.json':
                check('comparison_nested_reviews',len(r['checks']['mechanism_review_evidence_hashes'])==2 and all(not x['mismatches'] for x in r['checks']['mechanism_review_evidence_hashes']))
            for name,value in mapping.items():
                if not isinstance(value,str):continue
                source=Path(name) if Path(name).is_absolute() else ROOT/name
                if source.is_file():
                    okay=digest(source)==value;checked[str(source)]=okay
                    check(f'review_reference:{source}',okay)
                else:check(f'missing_review_reference:{source}',False)
        report['reviewed_reference_files']=len(checked)
        report['known_failures']=[
            'baseline/native_noise_burst: authenticated readiness missing; both inputs unplayed; blank received-ASR content',
            'candidate/ambiguous_road_then_hospital: valid initial clarification, but final lookup omits navigation activation and ETA',
            'baseline/delivery_delay5s_voiced_stop: old reply resumes during pending recognition and finishes',
            '11/21 trusted-trace equality failures; 12 missing native identities remain untrusted',
            '11 played non-speech probes do not activate VAD; acoustic empty-recognition recovery unexercised']
        report['decision']='Retain recognition-aware output guard with narrow controlled evidence; fix clarified navigation goal next.'
        report['goal_remaining']=['Clarified navigation request preservation','Concurrent RTC rooms','Acoustic policy/acknowledgment ablations','Human microphone/echo/physical playback','Organizer requirements and fresh adapted FDB evaluation','User-selected staging and PR publication']
        git=subprocess.run(['git','diff','--check'],cwd=ROOT,text=True,capture_output=True)
        report['whitespace_check']=dict(exit_code=git.returncode,stdout=git.stdout,stderr=git.stderr)
        check('whitespace',git.returncode==0)
        staged=subprocess.check_output(['git','diff','--cached','--name-only'],cwd=ROOT,text=True).splitlines()
        report['git_status']=dict(staged_paths=staged,branch=subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),commits_ahead=int(subprocess.check_output(['git','rev-list','--count','origin/main..HEAD'],cwd=ROOT,text=True)),pr_status=read(BASE/'pr-status.json'))
        methods=[p for p in BASE.rglob('*.py') if 'source-snapshot' not in p.parts and '__pycache__' not in p.parts]
        for path in methods:ast.parse(path.read_text(),filename=str(path))
        report['parsed_experiment_methods']=len(methods)
        paths=set()
        for directory in (BASE,ROOT/'results/iteration18-baseline',ROOT/'results/iteration18-candidate'):
            paths.update(p for p in directory.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc' and p!=output and p.name!='final-validation-output.txt')
        paths.update(ROOT/p for p in ('README.md','docs/iteration18.md','docs/completion-audit.md'))
        paths.add(ROOT.parent/'prism-pr-description.md')
        report['artifact_sha256']={str(p):digest(p) for p in sorted(paths)}
        check('artifact_inventory_stable',all(digest(p)==h for p,h in report['artifact_sha256'].items()))
        report['status']='checkpoint_verified_with_known_failures' if not report['issues'] else 'failed'
    except BaseException as error:
        report.update(status='failed',error=repr(error));raise
    finally:
        report['finished_at_utc']=stamp();save(output,report)
    print(json.dumps(dict(status=report['status'],issues=report['issues'],artifacts=len(report['artifact_sha256']),checks=len(report['checks']),task_complete=report['task_complete'])))
    return int(report['status']!='checkpoint_verified_with_known_failures')

if __name__=='__main__':raise SystemExit(main())
