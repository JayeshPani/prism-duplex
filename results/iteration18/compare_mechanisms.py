"""Post-run mechanism comparison; no model loads or changes to frozen evidence."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / 'results/iteration18'
HASHES = {}

def read(path):
    path = Path(path)
    data = path.read_bytes()
    HASHES[str(path.relative_to(ROOT))] = sha256(data).hexdigest()
    return json.loads(data)

def delta(end, start):
    return None if end is None or start is None else round((end-start)*1000, 6)

def main():
    reviews = {arm: read(ROOT/f'results/iteration18-{arm}/{arm}-mechanism-review.json') for arm in ('baseline', 'candidate')}
    source_verification = []
    for arm, review in reviews.items():
        mismatches = []
        for name, expected in review['evidence_sha256'].items():
            actual = sha256((ROOT/name).read_bytes()).hexdigest()
            if actual != expected:
                mismatches.append({'path': name, 'expected': expected, 'actual': actual})
        source_verification.append({'arm': arm, 'verified_evidence_files': len(review['evidence_sha256']), 'mismatches': mismatches})
    audit = read(BASE/'acoustic-audit.json')
    audited = {a['arm_id']: {c['case_id']: c for c in a['cases']} for a in audit['arms']}
    review_cases = {a: {c['case_id']: c for c in r['cases']} for a, r in reviews.items()}
    pairs = []
    for case_id, baseline in review_cases['baseline'].items():
        row = {'case_id': case_id, 'arms': {}}
        declared_hashes = []
        for arm in ('baseline', 'candidate'):
            case = review_cases[arm][case_id]
            full = audited[arm][case_id]
            capture = full['capture_report']
            probe = next(i['capture'] for i in full['inputs'] if i['capture']['label']=='probe')
            start = probe.get('timing', {}).get('started_monotonic')
            activity = None if start is None or probe.get('activity_start_sample') is None else start+probe['activity_start_sample']/probe['sample_rate']
            old = case['original_result_handle']
            old_id = old['speech_id'] if old else None
            next_queues = [h['queued_at'] for h in capture['speech_handles'] if old and h['speech_id']!=old_id and h.get('queued_at',-1)>old['queued_at']]
            boundary = min(next_queues) if next_queues else None
            path = ROOT/full['pcm']['raw_frame_source']
            raw = path.read_bytes()
            HASHES[str(path.relative_to(ROOT))] = sha256(raw).hexdigest()
            frames = [{'line':n, **json.loads(s)} for n,s in enumerate(raw.splitlines(),1)]
            loud = [f for f in frames if f['rms_db']>=capture['config']['threshold_db']]
            tagged = [f for f in loud if old_id and f.get('heuristic_result_speech_id')==old_id]
            broad = [f for f in loud if old and f['received_monotonic']>=old['queued_at'] and (boundary is None or f['received_monotonic']<boundary)]
            tails = {}
            for name, selected in (('handle_tagged',tagged),('broad_event_order_window',broad)):
                last = selected[-1] if selected else None
                tails[name] = {'last_loud_frame':last, 'from_publication_onset_ms':delta(last['received_monotonic'] if last else None,start), 'from_source_activity_onset_ms':delta(last['received_monotonic'] if last else None,activity)}
            frozen = full.get('probe_pcm_timing') or {}
            frozen_onset = activity if activity is not None else start
            recomputed_frozen = delta(broad[-1]['received_monotonic'] if broad else None, frozen_onset)
            frozen_value = frozen.get('last_old_pcm_after_onset_ms')
            frozen_matches = (round(recomputed_frozen,3) if recomputed_frozen is not None else None)==frozen_value
            sdk = []
            for event in case['sdk_false_interruption']:
                at = event['received_monotonic']
                later = [f for f in broad if at is not None and f['received_monotonic']>=at]
                sdk.append({**event, 'broad_window_loud_frames_after_resume':len(later), 'first_broad_loud_after_resume':later[0] if later else None})
            row['arms'][arm] = {
                'capture_status':case['status'], 'failed_stage':case['failed_stage'],
                'probe_published':start is not None, 'publication_onset_monotonic':start,
                'source_activity_onset_monotonic':activity, 'source_activity_offset_ms':delta(activity,start),
                'old_handle_state':old['state'] if old else None, 'old_handle_end_from_publication_ms':case['old_handle_end_after_probe_ms'],
                'old_handle_end_from_source_activity_ms':delta(old.get('ended_at') if old else None,activity),
                'interruption_request_from_source_activity_ms':delta(old.get('ended_at') if old and old['state']=='interruption_requested' else None,activity),
                'next_speech_queue_boundary':boundary, 'tail_definitions':tails,
                'frozen_audit_tail_ms':frozen_value,'frozen_audit_onset':'source_activity' if activity is not None else 'publication_fallback',
                'recomputed_frozen_tail_matches':frozen_matches,
                'old_finish_before_probe_final_ms':case['old_finish_before_probe_final_ms'],
                'post_probe_recognition_calls':case['post_probe_recognition_calls'],
                'post_probe_speaking_events':[e for e in case['post_probe_user_states'] if e['state']=='speaking'],
                'post_probe_nonempty_finals':case['post_probe_finals'],
                'sdk_false_interruption':sdk, 'recognition_pause_events':case.get('recognition_pause_events',[]),
                'recognition_calls':case['recognition_calls'],
                'successful_effects':len(case['effects']), 'physical_attempts':len(case['attempts']),
                'trusted_trace_equal':case['trusted_trace']['trusted_exact_sequence_equal'],
                'raw_trace_equal':case['trusted_trace']['raw_canonical_exact_sequence_equal'],
                'missing_native_identity_packets':len(case['trusted_trace']['missing_native_identity']),
            }
            declared_hashes.append([i['capture']['sha256'] for i in full['inputs']])
        row['prepared_input_hashes_match_between_arms'] = declared_hashes[0]==declared_hashes[1]
        pairs.append(row)
    report = {
      'created_at_utc':datetime.now(timezone.utc).isoformat(),
      'scope':'Post-run mechanism comparison of all eight matched baseline/candidate probe cases, with five additional candidate language cases retained in candidate-mechanism-review.json. No models, ASR, or physical speaker measurements run here.',
      'status':'complete_with_failures_preserved',
      'definitions':{
        'publication_onset':'Recorder input started_monotonic; includes leading source PCM silence.',
        'source_activity_onset':'Publication onset plus predeclared activity_start_sample/sample_rate. Silence has no source activity; retain null rather than invent an onset.',
        'handle_tagged':'Only loud frames carrying the matching heuristic_result_speech_id. This explicit field is still a recorder heuristic, not authenticated frame-to-speech metadata.',
        'broad_event_order_window':'All loud frames from the old handle queue until the next different speech queue, including untagged frames, matching frozen audit_acoustic.py. Data/RTP ordering is heuristic and may overattribute adjacent transport audio.',
        'loud':'Received frame RMS at or above recorder threshold_db (-40 dB in these captures).',
        'timer':'SDK resumed=true is its resume decision; guard held proves interception. Neither directly timestamps native sink resume or device sound.',
      },
      'matched_cases':pairs,
      'summary':{arm:r['summary'] for arm,r in reviews.items()},
      'checks':{
        'all_16_frozen_pcm_tails_independently_reproduced':all(p['arms'][a]['recomputed_frozen_tail_matches'] for p in pairs for a in ('baseline','candidate')),
        'all_8_prepared_input_pairs_byte_hash_match':all(p['prepared_input_hashes_match_between_arms'] for p in pairs),
        'mechanism_review_evidence_hashes':source_verification,
        'no_acoustic_audit_extraction_issues':not audit['evidence_issues'],
      },
      'bounded_conclusions':[
        'Native voiced-stop broad-window tails from source activity were 700.531 ms baseline and 679.740 ms candidate. Both old handles requested interruption, with no false-interruption callback or guard hold observed.',
        'With the controlled five-second delivery hold, broad-window tails from source activity were 7580.820 ms baseline and 699.443 ms candidate. Baseline SDK resumed while recognition remained pending and 129 handle-tagged loud frames subsequently arrived; the original result finished before final stop delivery. Candidate held before that same SDK resume decision, retained the hold through recognition, interrupted on nonempty text, then released on speech_finished; no broad-window loud frames arrived after the SDK resume receipt.',
        'Publication-onset and handle-tagged-only results differ from the frozen method. They are retained alongside it rather than substituted. In particular, untagged late PCM extends the native baseline tail and both candidate stop tails.',
        'All six candidate non-speech probes and five published baseline non-speech probes produced no post-probe speaking or recognition. Original handles finished, but false-pause recovery after an empty recognition was not exercised. Baseline native_noise_burst never published either input because session readiness timed out; it remains failed.',
        'Candidate ordinary language cases are 4/5 backend success: ambiguity clarification returned a unique hospital search but omitted compute/start, producing no navigation effect. The 1200-ms pause case produced two final transcripts, two whole-span recognition calls across the candidate run, and one correct destination effect; this does not assess received speech meaning.',
        'The study has one fixed-order run per arm and one sample per condition. Native engine times differ and the five-second hold is synthetic. The mechanism is observed here; historical stalls, broad reliability, physical audible timing, memory causation, and generalized multilingual/disfluency quality remain unestablished.',
      ],
      'cleanup_and_resources':{a:{'cleanup':r['cleanup'],'memory':r['memory'],'watchdogs':r['watchdogs'],'warning_counts':{k:len(v) for k,v in r['warnings'].items()},'client_warning_count':len(r['client_warning_or_traceback_lines']),'error_lines':r['error_lines'],'server_job_end_count':len(r['server_job_end_lines'])} for a,r in reviews.items()},
      'provenance_note':'The candidate extractor preparation initially encountered a generator syntax error before creating its method/output; no frozen/raw data was changed. The extraction method and successful receipt were then created and hashed. Previous mechanism receipts are preserved unchanged.',
    }
    HASHES[str(Path(__file__).relative_to(ROOT))]=sha256(Path(__file__).read_bytes()).hexdigest()
    report['evidence_sha256']=HASHES
    output=BASE/'combined-mechanism-comparison.json'
    with output.open('x') as f:json.dump(report,f,indent=2);f.write('\n')
    print(json.dumps({'output':str(output),'sha256':sha256(output.read_bytes()).hexdigest(),'checks':report['checks']}))

if __name__=='__main__':main()
