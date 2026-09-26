"""Verify immutable sources and owned cleanup for the explicitly partial protocol."""
import importlib.util
import json
import os
from pathlib import Path
import sys

from common import BASE, ROOT, digest, save, stamp, absent


def main():
    target=BASE/'post-inference-validation-v2.json'
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
            phase=read(BASE/(f'{name}-supervision-v2.json' if name=='candidate' else f'{name}-supervision.json'));report['phases'][name]=phase
            require(phase['status']=='exited' and phase.get('finished_at_utc') is not None,f'{name} nonterminal')
            require(phase.get('exit_code') is not None,f'{name} exit not observed')
            report['pids'].append(dict(role=name,pid=phase['pid']))
        generation=read(BASE/'input-generation/report.json')
        require(generation['status']=='completed' and bool(generation.get('finished_at_utc')) and not generation.get('cleanup_error') and report['phases']['inputs'].get('exit_code')==0, 'input generation or cleanup failed')
        protocol=read(BASE/'protocol.json')
        for declaration in protocol['arms']:
            arm=declaration['id'];base=ROOT/'results'/f'iteration20-{arm}'
            suite=read(base/'run-report.json');review=read(base/'received-audio-review/report.json')
            stack=base/'local-stack-run1'
            selection=read(stack/'experiment-worker-selection.json')
            worker_exit=read(stack/'experiment-worker-exit.json')
            diagnostic_exit=read(stack/'stt-diagnostics-exit.json')
            plan=read(base/'plan.json')
            package=BASE/'baseline-package' if arm=='baseline' else ROOT
            report.setdefault('worker_evidence',{})[arm]=dict(selection=selection,exit=worker_exit,
                                                            diagnostic_exit=diagnostic_exit)
            require(selection['policy']==worker_exit['policy']==arm and
                    selection['agent_package_root']==worker_exit['agent_package_root']==str(package) and
                    selection['source_variant']==plan['source_variant'],f'{arm} selected package differs')
            require(selection['plan_sha256']==digest(base/'plan.json') and
                    selection['protocol_sha256']==digest(BASE/'protocol.json') and
                    selection['configuration']==protocol['configuration'] and
                    selection['turn_handling']==protocol['turn_handling'],f'{arm} worker launch binding differs')
            tts_paths={str(ROOT/'models/kokoro'/name):frozen['model_files'][str(ROOT/'models/kokoro'/name)]['reference']
                       for name in ('kokoro-v1.0.onnx','voices-v1.0.bin')}
            require(selection['kokoro_directory']['selected']==str(ROOT/'models/kokoro') and
                    selection['kokoro_directory']['reference_sha256']==tts_paths,f'{arm} TTS resource binding differs')
            for name,receipt in (('worker',worker_exit),('diagnostics',diagnostic_exit)):
                require(receipt.get('diagnostics_saved') is True and not receipt.get('diagnostic_error') and
                        receipt.get('initial_modules_unchanged') is True and not receipt.get('module_provenance_errors')
                        and receipt.get('kokoro_directory_at_exit')==str(ROOT/'models/kokoro')
                        and receipt.get('kokoro_directory_unchanged') is True,
                        f'{arm} {name} diagnostics/provenance failed')
            before,after=selection['loaded_agent_modules'],worker_exit['loaded_agent_modules']
            require(bool(before) and before.keys()<=after.keys() and
                    all(after[n]==r for n,r in before.items()),f'{arm} loaded module inventory changed')
            require(diagnostic_exit['loaded_agent_modules']==after,f'{arm} diagnostic exit inventory differs')
            require(all(Path(r['path']).is_relative_to(package/'agent') and
                        frozen['source_sha256'].get(r['path'])==r['sha256'] for r in after.values()),
                    f'{arm} runtime module source differs from frozen package')
            manifest=read(stack/'worker-manifest.json')
            require(all(frozen['source_sha256'].get(p)==h for p,h in manifest['source_sha256'].items()),
                    f'{arm} worker manifest source missing or changed in freeze')
            require(suite['status'] not in {'starting','running'} and bool(suite.get('finished_at')),f'{arm} suite incomplete')
            expected_attempted = declaration['cases'][:2] if arm == 'baseline' else declaration['cases']
            require([r['case'] for r in suite['runs']]==expected_attempted,f'{arm} actual attempt inventory differs')
            if arm == 'baseline':
                require(suite['status']=='failed' and suite['runs'][0]['exit_code']==0 and suite['runs'][1]['exit_code']==2
                        and not (base/suite['runs'][1]['name']/'report.json').exists(), 'baseline partial failure inventory differs')
                require('--followup-trigger response-finished requires --followup' in (base/(suite['runs'][1]['name']+'.log')).read_text(), 'baseline parser failure not retained')
            else:
                require(suite['status']=='completed' and suite['all_agents_departed'] and all(r['exit_code']==0 for r in suite['runs']), 'candidate captures incomplete')
            require(suite.get('frozen_inputs_unchanged') is True,f'{arm} frozen source/input changed')
            ports=suite['port_availability']
            require(ports['status']=='available' and ports['deadline_seconds']==90
                    and ports['polls'][-1]['all_available'] is True
                    and all(len(p['ports'])==4 and {r['port'] for r in p['ports']}=={7880,7882,8081,8082}
                            and p['all_available']==all(r['bind_available'] for r in p['ports']) for p in ports['polls']),
                    f'{arm} port observations incomplete')
            require(review['status'] not in {'running','starting'} and bool(review.get('finished_at_utc')),f'{arm} ASR incomplete')
            require([r['case'] for r in review['trials']]==declaration['cases'],f'{arm} ASR denominator changed')
            require(set(r['role'] for r in suite['cleanup'])=={'livekit','llm','worker'},f'{arm} cleanup roles missing')
            for row in suite['cleanup']:
                launch=read(base/'local-stack-run1'/f"{row['role']}-launch.json")
                require(ports['finished_at']<=launch['started_at'],f'{arm} service launched before port observation finished')
                require(row['pid']==launch['pid'] and row.get('exit_code') is not None,f'{arm} {row["role"]} exit missing')
                report['pids'].append(dict(role=f"{arm}/{row['role']}",pid=row['pid']))
            for row in suite['runs']:
                report['pids'].append(dict(role=f"{arm}/{row['name']}",pid=row['pid']))
            require(report['phases'][f'asr-{arm}']['started_at_utc'] > max(
                read(ROOT/'results'/f'iteration20-{a}'/'run-report.json')['finished_at'] for a in ('baseline','candidate')),f'{arm} ASR overlapped timed arm')
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
        amendment=read(BASE/'harness-amendment.json')
        report['harness_amendment']=amendment
        require(all(digest(p)==h for p,h in amendment['new_method_sha256'].items()), 'amended methods changed')
        report['all_phases_reached']=True
        report['full_declared_protocol_completed']=False
        report['observed_scope']={'declared_cases':12,'completed_captures':7,'parser_failed_clients':1,'unlaunched_cases':4,'matched_pairs':1}
        report['limit']='Byte/provenance and cleanup verification for a partial experiment. The six-case-per-arm comparison was not completed.'
        report['status']='passed' if not report['issues'] else 'failed'
    except BaseException as error:
        report.update(status='failed',error=repr(error));raise
    finally:
        report['finished_at_utc']=stamp();save(target,report)
    print(json.dumps(dict(status=report['status'],issues=report['issues'],models=len(report['model_files']),sources=len(report['source_files']),pids=len(report['pids']))))
    return int(report['status']!='passed')

if __name__=='__main__':raise SystemExit(main())
