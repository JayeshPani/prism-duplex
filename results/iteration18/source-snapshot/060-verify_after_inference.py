"""Fail-closed shared post-check after every frozen experiment phase exits."""
import importlib.util
import json
import os
from pathlib import Path
import sys

from common import BASE, ROOT, digest, save, stamp, absent


def main():
    target=BASE/'post-inference-validation.json'
    report=dict(status='running',started_at_utc=stamp(),issues=[],phases={},model_files={},source_files={},snapshot_files={},pids=[])
    save(target,report,exclusive=True)
    def require(ok,message):
        if not ok:report['issues'].append(message)
        return ok
    def read(path):
        return json.loads(Path(path).read_text())
    try:
        frozen=read(BASE/'preflight.json')
        require(frozen['status']=='passed','preflight not passed')
        source=ROOT/'results/iteration17/verify_after_inference.py'
        spec=importlib.util.spec_from_file_location('prior_stable_reader',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        require(digest(source)==frozen['source_sha256'][str(source)],'stable reader changed')
        for name in ('inputs','baseline','candidate','asr-baseline','asr-candidate'):
            phase=read(BASE/f'{name}-supervision.json');report['phases'][name]=phase
            require(phase['status']=='exited' and phase.get('finished_at_utc') is not None,f'{name} nonterminal')
            require(phase.get('exit_code') is not None,f'{name} exit not observed')
            report['pids'].append(dict(role=name,pid=phase['pid']))
        generation=read(BASE/'input-generation/report.json')
        require(generation['status']=='completed' and bool(generation.get('finished_at_utc')) and not generation.get('cleanup_error') and report['phases']['inputs'].get('exit_code')==0, 'input generation or cleanup failed')
        protocol=read(BASE/'protocol.json')
        for declaration in protocol['arms']:
            arm=declaration['id'];base=ROOT/'results'/f'iteration18-{arm}'
            suite=read(base/'run-report.json');review=read(base/'received-audio-review/report.json')
            require(suite['status'] not in {'starting','running'} and bool(suite.get('finished_at')),f'{arm} suite incomplete')
            require([r['case'] for r in suite['runs']]==declaration['cases'],f'{arm} declared cases not all attempted in order')
            require(suite.get('frozen_inputs_unchanged') is True,f'{arm} frozen source/input changed')
            require(review['status'] not in {'running','starting'} and bool(review.get('finished_at_utc')),f'{arm} ASR incomplete')
            require([r['case'] for r in review['trials']]==declaration['cases'],f'{arm} ASR denominator changed')
            require(set(r['role'] for r in suite['cleanup'])=={'livekit','llm','worker'},f'{arm} cleanup roles missing')
            for row in suite['cleanup']:
                launch=read(base/'local-stack-run1'/f"{row['role']}-launch.json")
                require(row['pid']==launch['pid'] and row.get('exit_code') is not None,f'{arm} {row["role"]} exit missing')
                report['pids'].append(dict(role=f"{arm}/{row['role']}",pid=row['pid']))
            for row in suite['runs']:
                report['pids'].append(dict(role=f"{arm}/{row['name']}",pid=row['pid']))
            require(report['phases'][f'asr-{arm}']['started_at_utc'] > max(
                read(ROOT/'results'/f'iteration18-{a}'/'run-report.json')['finished_at'] for a in ('baseline','candidate')),f'{arm} ASR overlapped timed arm')
        report['pids_before_models']=[dict(x,absent=absent(x['pid'])) for x in report['pids']]
        require(all(x['absent'] for x in report['pids_before_models']),'owned PID still present before model reads')
        if report['issues']:
            raise RuntimeError('Prerequisites failed; do not read weights while experiment state is unresolved')
        require(len(frozen['required_runtime_sources'])==11 and all(p in frozen['source_sha256'] for p in frozen['required_runtime_sources']), 'runtime source census incomplete')
        for path,expected in frozen['source_sha256'].items():
            row=module.read_digest(path,expected);report['source_files'][path]=row;require(row['matches'],f'source mismatch {path}')
        require(digest(BASE/'source-snapshot.json')==frozen['snapshot_sha256'],'snapshot receipt changed')
        snapshots=read(BASE/'source-snapshot.json')['files']
        require(len(snapshots)==len(frozen['source_sha256']) and {r['original']:r['sha256'] for r in snapshots}==frozen['source_sha256'], 'snapshot/source census differs')
        require(len({r['snapshot'] for r in snapshots})==len(snapshots),'duplicate snapshot path')
        if report['issues']:
            raise RuntimeError('Frozen sources or snapshot census failed before model reads')
        for item in snapshots:
            row=module.read_digest(item['snapshot'],item['sha256']);report['snapshot_files'][item['snapshot']]=row
            require(row['matches'],f'snapshot mismatch {item["snapshot"]}')
        if report['issues']:
            raise RuntimeError('Snapshot bytes failed before model reads')
        for path,item in frozen['model_files'].items():
            row=module.read_digest(path,item['reference']);report['model_files'][path]=row;require(row['matches'],f'model mismatch {path}')
        assets=read(BASE/'input-assets.json')
        report['input_files']={name:module.read_digest(BASE/'audio-assets'/name,item['sha256']) for name,item in assets['assets'].items()}
        require(all(r['matches'] for r in report['input_files'].values()),'input assets changed')
        report['pids_after_models']=[dict(x,absent=absent(x['pid'])) for x in report['pids']]
        require(all(x['absent'] for x in report['pids_after_models']),'owned PID present after model reads')
        require(len(report['model_files'])==10,'model census incomplete')
        require(len(report['source_files'])==len(frozen['source_sha256']),'source census incomplete')
        report['all_phases_reached']=True
        report['status']='passed' if not report['issues'] else 'failed'
    except BaseException as error:
        report.update(status='failed',error=repr(error));raise
    finally:
        report['finished_at_utc']=stamp();save(target,report)
    print(json.dumps(dict(status=report['status'],issues=report['issues'],models=len(report['model_files']),sources=len(report['source_files']),pids=len(report['pids']))))
    return int(report['status']!='passed')

if __name__=='__main__':raise SystemExit(main())
