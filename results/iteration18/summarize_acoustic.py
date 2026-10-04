"""Post-freeze reporting helper: retain all cases and separate semantic review.

Written after baseline execution began; does not redefine frozen criteria.
"""
import json
from pathlib import Path
from common import BASE, digest, save, stamp


def main():
    audit_path=BASE/'acoustic-audit.json'
    audit=json.loads(audit_path.read_text())
    rows=[]
    for arm in audit['arms']:
        for case in arm['cases']:
            pcm=case.get('probe_pcm_timing',{})
            speech=case.get('speech_review',{})
            criteria={**case.get('criteria',{}),**case.get('effect_criteria',{})}
            rows.append(dict(arm=arm['arm_id'],case=case['case_id'],extraction_status=case['status'],
                capture_status=case.get('capture_report',{}).get('status'),
                played_inputs=sum(x.get('capture',{}).get('timing',{}).get('published_samples',0)>0 for x in case.get('inputs',[]) if x.get('capture')),
                expected_inputs=len(case['declaration']['inputs']),
                expected_effects=len(case['declaration']['expected']['effects']),
                observed_effects=len(case.get('execution',{}).get('successful_mutations',[])),
                physical_attempts=len(case.get('execution',{}).get('physical_calls',[])),
                checks={name:r['status'] for name,r in criteria.items()},
                live_finals=case.get('live_transcripts_and_merges',[]),
                declared_speech=[e['data'] for e in speech.get('declared_agent_text',[])],
                received_speech=[{k:r.get(k) for k in ('status','attempted','transcript','error','failed_stage','stt_seconds')} for r in speech.get('received_asr_rows',[])],
                old_handle_state=(pcm.get('old_handle') or {}).get('state'),
                probe_timing={k:pcm.get(k) for k in ('interruption_request_after_onset_ms','last_old_pcm_after_onset_ms','first_300ms_quiet_after_onset_ms')},
                recovery_exercised=case.get('probe_observations',{}).get('recovery_exercised')))
    out=dict(created_at_utc=stamp(),method_sha256=digest(__file__),audit_sha256=digest(audit_path),
             method_limit='Post-freeze reporting helper; frozen auditor criteria unchanged; all semantic speech reviews remain manual.',rows=rows)
    save(BASE/'acoustic-summary.json',out,exclusive=True)
    print(json.dumps(rows,indent=2))

if __name__=='__main__':main()
