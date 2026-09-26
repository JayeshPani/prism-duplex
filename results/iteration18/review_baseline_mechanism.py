"""Post-run baseline-only extraction; no candidate files, models, services or ASR."""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import base64
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'results/iteration18-baseline'
STACK=BASE/'local-stack-run1'
SOURCES={}

def read(path):
    raw=path.read_bytes();SOURCES[str(path.relative_to(ROOT))]=sha256(raw).hexdigest();return raw.decode()

def js(path):return json.loads(read(path))
def lines(path):return [json.loads(x) for x in read(path).splitlines() if x.strip()]
def canon(event):return {key:event[key] for key in ('type','data','ts')}
def key(event):return json.dumps(canon(event),sort_keys=True,separators=(',',':'))
def ms_delta(a,b):return None if a is None or b is None else (a-b)*1000


def main():
    output=BASE/'baseline-mechanism-review.json'
    assert not output.exists()
    run=js(BASE/'run-report.json');assert run.get('finished_at') and run['status']=='completed_with_failed_captures'
    protocol=js(BASE/'protocol.json');global_protocol=js(ROOT/'results/iteration18/protocol.json')
    declared={x['id']:x for x in global_protocol['cases']}
    delay=js(STACK/'recognition-delivery-diagnostics.json');stt=js(STACK/'stt-diagnostics/report.json');aec=js(STACK/'aec-discard-diagnostics.json')
    selection=js(STACK/'experiment-worker-selection.json')
    attempts=lines(STACK/'tool-calls.jsonl');memory=lines(BASE/'memory.jsonl')
    cases=[];binding_checks=[];condition_checks=[]
    for entry in run['runs']:
        name=entry['case'];directory=BASE/entry['name'];capture=js(directory/'report.json');room=capture['room']
        events=lines(directory/'events.jsonl');frames=lines(directory/'frames.jsonl');trace_path=STACK/'traces'/f'{room}.jsonl';server=lines(trace_path)
        condition_path=STACK/'conditions'/f"{entry['name']}.json";condition=js(condition_path)
        expected_delay=declared[name].get('recognition_delivery_delay_seconds',0)
        condition_ok=condition==entry['condition']=={'case_id':name,'delay_seconds':expected_delay} and SOURCES[str(condition_path.relative_to(ROOT))]==entry['condition_sha256']
        condition_checks.append(condition_ok)
        bindings=[x for x in delay['bindings'] if x['room']==room]
        binding_ok=all(x['case_id']==name and x['delay_seconds']==expected_delay and x['condition_sha256']==entry['condition_sha256'] for x in bindings)
        binding_checks.append(binding_ok)
        inputs=[]
        for item in capture['inputs']:
            timing=item.get('timing');inputs.append({'label':item['label'],'prepared_sha256':item['sha256'],'published':bool(timing and timing.get('published_samples',0)>0),'timing':timing})
        probe=next((x for x in capture['inputs'] if x['label']=='probe'),None)
        probe_start=probe.get('timing',{}).get('started_monotonic') if probe else None
        promoted=[x for x in events if x['kind']=='coordinator_event'];trusted=[x['event'] for x in promoted]
        raw=[];raw_errors=[];missing_identity=[]
        for i,event in enumerate(events,1):
            if event['kind']!='data_packet' or event.get('topic')!='agent-events':continue
            try:decoded=json.loads(base64.b64decode(event['payload_base64']));raw.append(decoded)
            except Exception as error:raw_errors.append({'line':i,'error':repr(error)});continue
            if not event.get('native_participant_identity'):
                missing_identity.append({'line':i,'packet_id':event['packet_id'],'public_participant':event.get('participant'),'native_identity':event.get('native_participant_identity'),'canonical_event':canon(decoded),'matches_server_event':key(decoded) in [key(x) for x in server]})
        trusted_missing=list((Counter(map(key,server))-Counter(map(key,trusted))).elements())
        after=[x for x in promoted if probe_start is not None and x['received_monotonic']>=probe_start]
        user_states=[{'line':i+1,'server_ts':x['event']['ts'],'received_monotonic':x['received_monotonic'],'after_probe_ms':ms_delta(x['received_monotonic'],probe_start),'state':x['event']['data']['state']} for i,x in enumerate(promoted) if x in after and x['event']['type']=='user_state']
        post_calls=[x for x in delay['calls'] if x['room']==room and probe_start is not None and x['entry']['monotonic_ns']/1e9>=probe_start]
        all_calls=[]
        for call in [x for x in delay['calls'] if x['room']==room]:
            matches=[c for c in stt['calls'] if c.get('room')==room and c.get('call_type')=='_recognize_impl' and call['entry']['monotonic_ns']<=c['engine_api_entry']['monotonic_ns']<=call['original_engine_return']['monotonic_ns']]
            assert len(matches)==1
            native=matches[0];request=next(x for x in stt['requests'] if x['request_id']==native['request_id'])
            assert native['caller_return']['monotonic_ns']<=call['original_engine_return']['monotonic_ns']
            all_calls.append({'delivery_call':call,'stt_call_id':native['call_id'],'request_id':native['request_id'],'original_text':native['caller_return'].get('result_text'),'native_engine_api_ms':(native['caller_return']['monotonic_ns']-native['engine_api_entry']['monotonic_ns'])/1e6,'request_wall_ms':(request['return']['monotonic_ns']-request['entry']['monotonic_ns'])/1e6,'synthetic_hold_ms':(call['hold_end']['monotonic_ns']-call['hold_start']['monotonic_ns'])/1e6 if 'hold_end'in call else None,'probe_recognition':call in post_calls})
        old=capture.get('probe_observation',{}).get('original_handle');old_id=old.get('speech_id') if old else None
        old_loud=[x for x in frames if old_id and x.get('heuristic_result_speech_id')==old_id and x['rms_db']>=capture['config']['threshold_db']]
        false_rows=[]
        for i,event in enumerate(server,1):
            if event['type']!='agent_false_interruption':continue
            client=next((x for x in promoted if key(x['event'])==key(event)),None)
            ts=event['ts'];at=client['received_monotonic'] if client else None
            holds=[x['call_id'] for x in delay['calls'] if x['room']==room and x.get('hold_start',{}).get('unix_ns',float('inf'))/1e9<=ts<=x.get('hold_end',{}).get('unix_ns',float('-inf'))/1e9]
            pending=[x['call_id'] for x in delay['calls'] if x['room']==room and x['entry']['unix_ns']/1e9<=ts<=x['delivery_return']['unix_ns']/1e9]
            preceding_eos=[x['ts'] for x in server[:i-1] if x['type']=='user_state' and x['data'].get('state')=='listening']
            before=[f for f in old_loud if at is not None and f['received_monotonic']<at];after_resume=[f for f in old_loud if at is not None and f['received_monotonic']>=at]
            false_rows.append({'server_line':i,'server_ts':ts,'resumed':event['data']['resumed'],'received_monotonic':at,'after_probe_ms':ms_delta(at,probe_start),'after_preceding_user_listening_ms':ms_delta(ts,preceding_eos[-1] if preceding_eos else None),'within_delivery_hold_call_ids':holds,'pending_delivery_call_ids':pending,'last_old_loud_before_resume':before[-1]['received_monotonic'] if before else None,'first_old_loud_after_resume':after_resume[0]['received_monotonic'] if after_resume else None,'old_loud_frames_after_resume':len(after_resume)})
        finals=[{'server_line':i,'ts':x['ts'],**x['data']} for i,x in enumerate(server,1) if x['type']=='user_final']
        probe_finals=[x for x in after if x['event']['type']=='user_final']
        old_finish=next((x['ts'] for x in server if x['type']=='speech_handle' and x['data'].get('speech_id')==old_id and x['data'].get('state')=='finished'),None)
        effects=[{'server_line':i,'ts':x['ts'],**x['data']} for i,x in enumerate(server,1) if x['type']=='tool_done' and x['data'].get('tool') in ('start_navigation','cancel_navigation','add_waypoint')]
        room_attempts=[x for x in attempts if x['room']==room]
        aec_rows=[x for x in aec['recognition_objects'] if x.get('room')==room]
        overlap=[]
        if probe_start is not None:
            end=probe['timing']['drained_monotonic']
            for row in aec_rows:
                for interval in row['intervals']:
                    hi=interval.get('next_unsubstituted_monotonic',interval['last_monotonic']);lo=interval['first_monotonic']
                    if lo<end and hi>probe_start:overlap.append(interval)
        cases.append({'case_id':name,'room':room,'status':capture['status'],'failed_stage':capture.get('failed_stage'),'error':capture.get('error'),'inputs':inputs,'condition_matches':condition_ok,'bindings':bindings,'binding_matches':binding_ok,'recognition_calls':all_calls,'post_probe_recognition_calls':len(post_calls),'post_probe_user_states':user_states,'post_probe_finals':[canon(x['event']) for x in probe_finals],'all_user_finals':finals,'original_result_handle':old,'last_old_pcm_after_probe_ms':ms_delta(old_loud[-1]['received_monotonic'] if old_loud else None,probe_start),'old_handle_end_after_probe_ms':ms_delta(old.get('ended_at') if old else None,probe_start),'old_finish_before_probe_final_ms':ms_delta(probe_finals[0]['event']['ts'] if probe_finals else None,old_finish),'sdk_false_interruption':false_rows,'candidate_hold_event_count':sum(x['type']=='recognition_pause' for x in server),'effects':effects,'attempts':room_attempts,'trusted_trace':{'server_count':len(server),'raw_count':len(raw),'trusted_count':len(trusted),'trusted_exact_sequence_equal':list(map(key,trusted))==list(map(key,server)),'raw_canonical_exact_sequence_equal':list(map(key,raw))==list(map(key,server)),'missing_trusted_events':[json.loads(x) for x in trusted_missing],'missing_native_identity':missing_identity,'decode_errors':raw_errors,'attribution_counts':capture['data_attribution_counts']},'aec_frames':sum(x['frames'] for x in aec_rows),'aec_substituted_frames':sum(x['substituted_frames'] for x in aec_rows),'aec_substitution_intervals':sum(len(x['intervals']) for x in aec_rows),'aec_probe_overlapping_intervals':overlap,'room_departure':entry.get('room_departure'),'capture_cleanup_errors':capture.get('cleanup_errors'), 'receiver_errors':capture.get('receiver_errors'), 'frame_count':len(frames)})
    worker=[];unparsed_worker=[]
    for i,line in enumerate(read(STACK/'worker.log').splitlines(),1):
        try:worker.append({'line':i,**json.loads(line)})
        except Exception:
            if line.strip():unparsed_worker.append({'line':i,'text':line})
    warning_logs={};error_logs={};jobs=[]
    for filename in ('worker.log','llm.log','livekit.log'):
        content=read(STACK/filename).splitlines()
        warning_logs[filename]=[{'line':i,'text':x} for i,x in enumerate(content,1) if re.search(r'"level": "WARNING"|\tWARN\t|\b\w*Warning:',x)]
        error_logs[filename]=[{'line':i,'text':x} for i,x in enumerate(content,1) if re.search(r'"level": "ERROR"|\tERROR\t|Traceback \(most recent call last\)',x)]
        if filename=='livekit.log':jobs=[{'line':i,'text':x} for i,x in enumerate(content,1) if '\tjob ended\t' in x]
    client_warnings=[]
    for entry in run['runs']:
        content=read(BASE/f"{entry['name']}.log").splitlines()
        client_warnings.extend({'file':f"{entry['name']}.log",'line':i,'text':x} for i,x in enumerate(content,1) if re.search(r'\b\w*Warning:|Traceback \(most recent call last\)',x))
    trace_cleanup=[]
    for x in worker:
        m=re.search(r'trace cleanup: (.*?) complete=(\w+) accepted=(\d+) flushed=(\d+) rejected=(\d+) pending=(\w+)',x.get('message',''))
        if m:trace_cleanup.append({'worker_line':x['line'],'timestamp':x['timestamp'],'path':m[1],'complete':m[2]=='True','accepted':int(m[3]),'flushed':int(m[4]),'rejected':int(m[5]),'pending':m[6]=='True'})
    statuses=Counter(x['call']['status'] for x in attempts);holds=[c for c in delay['calls'] if 'hold_end'in c]
    mem_errors=[{'line':i,'role':role,'value':value} for i,x in enumerate(memory,1) for role,value in x.get('owned_processes',{}).items() if not isinstance(value,list)]
    peak_rss={role:max((sum(p['rss'] for p in x['owned_processes'].get(role,[]) if isinstance(p,dict)) for x in memory if isinstance(x['owned_processes'].get(role),list)),default=0) for role in ('worker','llm','livekit')}
    report={'created_at_utc':datetime.now(timezone.utc).isoformat(),'status':'completed_baseline_only_read_only_review','scope':'All eight declared baseline cases; candidate files and received-audio ASR not inspected. Post-run mechanism extraction, not the final frozen 21-case acoustic audit or speech-meaning assessment.',
      'summary':{'declared_cases':8,'run_cases':len(run['runs']),'completed_probe_observations':sum(c['status']=='completed_probe_observation' for c in cases),'failed_captures':sum(c['status']=='failed' for c in cases),'declared_input_publications':16,'actual_input_publications':sum(sum(i['published'] for i in c['inputs']) for c in cases),'declared_probe_publications':8,'actual_probe_publications':sum(any(i['label']=='probe' and i['published'] for i in c['inputs']) for c in cases),'expected_effects':10,'observed_effects':sum(len(c['effects']) for c in cases),'physical_attempts':len(attempts),'attempt_statuses':dict(statuses),'duplicate_attempt_ids':len(attempts)-len({x['call']['attempt_id'] for x in attempts}),'stale_tool_done':sum(e.get('stale',False) for c in cases for e in c['effects']),'sdk_false_interruption_events':sum(len(c['sdk_false_interruption']) for c in cases),'candidate_hold_events':sum(c['candidate_hold_event_count'] for c in cases),'trusted_trace_equal_cases':sum(c['trusted_trace']['trusted_exact_sequence_equal'] for c in cases),'raw_canonical_equal_cases':sum(c['trusted_trace']['raw_canonical_exact_sequence_equal'] for c in cases),'server_events':sum(c['trusted_trace']['server_count'] for c in cases),'raw_events':sum(c['trusted_trace']['raw_count'] for c in cases),'trusted_events':sum(c['trusted_trace']['trusted_count'] for c in cases),'missing_native_identity_packets':sum(len(c['trusted_trace']['missing_native_identity']) for c in cases)},
      'interpretation':['native_noise_burst failed connect_and_session_ready before either input publication. Two received raw packets had empty native identity and were rejected; their canonical bodies match server agent_state/listening and session_started. Matching payload does not authenticate them. Case remains failed with zero effects and no recognition binding.',
      'Native voiced stop produced a nonempty final and interrupted the original result without a recorded false-resume event. The injected-five-second voiced stop produced one resumed=true callback inside its pending delivery hold, then old result finished before the final stop arrived. This directly observes the baseline timer/delivery ordering for this case; it does not identify why any historical native recognition was slow.',
      'Five non-speech probes were actually published; none caused post-probe user-speaking or recognition. Their original handles finished, but false-pause/empty-recognition recovery was not exercised. The sixth declared non-speech case never published.',
      'Every delayed room first recognition, and the delayed voiced-stop followup, received the declared hold. Only five holds occurred because non-speech probes did not cause recognition. Native inference durations and injected hold are kept separate.'],
      'delay_validation':{'policy':delay['policy'],'all8_runtime_conditions_match':all(condition_checks),'bindings':len(delay['bindings']),'binding_errors':delay['binding_errors'],'all_observed_bindings_match_exact_case_room_condition_hash':all(binding_checks),'room_recognition_calls':len(delay['calls']),'delayed_calls':len(holds),'hold_ms':[(c['hold_end']['monotonic_ns']-c['hold_start']['monotonic_ns'])/1e6 for c in holds],'pending_at_all_hold_boundaries':all(c['hold_start']['pending_recognitions']==c['hold_end']['pending_recognitions']==1 for c in holds),'all_calls_returned':all(c['status']=='returned' for c in delay['calls']),'sources_unchanged_reported':delay['source_hashes_unchanged'],'selection_policy':selection['policy']},
      'cases':cases,'diagnostics':{'stt_call_count':len(stt['calls']),'stt_call_types':dict(Counter(x['call_type'] for x in stt['calls'])),'stt_request_count':len(stt['requests']),'dropped_calls':stt['dropped_calls'],'dropped_requests':stt['dropped_requests'],'stt_observation_errors':stt['observation_error_count'],'pending_native_calls':stt['pending_native_call_ids'],'aec_observation_errors':aec['observation_error_count'],'aec_source_hashes_unchanged_reported':aec['source_hashes_unchanged'],'aec_probe_overlap_intervals':sum(len(c['aec_probe_overlapping_intervals']) for c in cases),'aec_identity_errors':[r['identity_error'] for r in aec['recognition_objects'] if r.get('identity_error')]},
      'warnings':warning_logs,'client_warning_or_traceback_lines':client_warnings,'error_lines':error_logs,'worker_unparsed_lines':unparsed_worker,'watchdogs':[x for x in worker if 'event loop blocked' in x.get('message','')],'server_job_end_lines':jobs,
      'cleanup':{'recorded_service_cleanup':run['cleanup'],'all_agents_departed':run['all_agents_departed'],'worker_clean_exit':run['worker_clean_exit'],'run_frozen_inputs_unchanged':run['frozen_inputs_unchanged'],'trace_cleanup':trace_cleanup,'application_cleanup_started':sum(x.get('message','').startswith('room cleanup started:') for x in worker),'application_cleanup_finished':sum(x.get('message','').startswith('room cleanup finished:') for x in worker),'fresh_pid_check':'Not performed here; root owns service supervision and absence receipts. Server JS_FAILED remains separate from application cleanup and worker exit0.'},
      'memory':{'samples':len(memory),'scope':run['memory_scope'],'first_sample':memory[0]['at_utc'],'last_sample':memory[-1]['at_utc'],'minimum_system_available_bytes':min(x['system_memory']['available'] for x in memory),'swap_first_bytes':memory[0]['swap']['used'],'swap_last_bytes':memory[-1]['swap']['used'],'swap_peak_bytes':max(x['swap']['used'] for x in memory),'owned_role_peak_combined_rss_bytes':peak_rss,'sample_errors':mem_errors,'causality':'No inference-delay cause or allocation/capacity conclusion follows from these samples.'},
      'limits':['Old-speech frame labels remain recorder heuristics across RTP/data channels; timestamps measure client frame arrival, not physical audibility.','SDK resumed=true records its resume decision. The baseline transparent output permits that call, but this receipt does not add a native sink instrumentation event.','Raw canonical trace equality is delivery evidence, not authentication of empty-identity packets. Trusted equality failures are preserved.','No candidate outcome, ASR transcript or final spoken-fact completeness is claimed.','Source/evidence hashing here excludes model weight files and received PCM; frames and recorder input hashes are retained as existing evidence.']}
    SOURCES[str(Path(__file__).relative_to(ROOT))]=sha256(Path(__file__).read_bytes()).hexdigest()
    report['evidence_sha256']=SOURCES
    with output.open('x') as f:json.dump(report,f,indent=2);f.write('\n')
    print(json.dumps({'output':str(output),'sha256':sha256(output.read_bytes()).hexdigest(),'summary':report['summary'],'delay_validation':report['delay_validation'],'memory':report['memory'],'warning_counts':{k:len(v) for k,v in warning_logs.items()},'client_warning_lines':len(client_warnings)}))

if __name__=='__main__':main()
