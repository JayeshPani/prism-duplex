"""Read-only derivation and manual text audit; refuses to replace its analysis."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def metrics(rows):
    values = sorted(row['elapsed_ms'] for row in rows if row['status'] == 'completed')
    return {
        'declared_rows_in_group': len(rows), 'completed_rows': len(values),
        'logical_model_calls': sum(row['logical_model_calls'] for row in rows),
        'zero_model_call_rows': sum(row['logical_model_calls'] == 0 for row in rows),
        'min_ms': min(values) if values else None, 'max_ms': max(values) if values else None,
        **{f'p{p}_ms': values[math.ceil(p / 100 * len(values)) - 1] if values else None for p in (50, 95)},
    }

comparison = json.loads((BASE / 'summary-comparison.json').read_text())
protocol = json.loads((BASE / 'summary-protocol.json').read_text())
inputs = json.loads((BASE / 'inputs.json').read_text())
requests = [json.loads(line) for line in (BASE / 'summary-model-requests.jsonl').read_text().splitlines()]
assert comparison['status'] == 'completed'
rows = comparison['trials']
cases = {case['id']: case for case in inputs['cases']}
expected = [(block, arm, case) for block, arm in enumerate(protocol['arm_order'], 1) for case in protocol['case_order']]
actual = [(row['block'], row['arm'], row['case']) for row in rows]
assert actual == expected and len(rows) == 40
original = set(protocol['expected_direct_cases'])
assert len(original) == 8 and 'via' in original
request_map = {}
for receipt in requests:
    request_map.setdefault((receipt['block'], receipt['arm'], receipt['case']), []).append(receipt)

# These observations were manually established by reading every text against
# the frozen request and execution, rather than by treating success as fidelity.
review_notes = {
    'start': 'Correct destination, completed start and 13-minute ETA.',
    'replace': 'Correct Whitefield (ITPL) destination and navigation change.',
    'via': 'Correct start, requested Blue Tokai stop and 6-minute ETA; hospital name is shortened to the user wording, omitting Old Airport Road. Route ID adds no requested information.',
    'already_active': 'Snapshot is already_active=true; Office route retains Ather Grid charger and ETA 29. A fresh start did not occur.',
    'add_stop': 'Blue Tokai is the first/nearest returned coffee result; actual added waypoint has ETA 13 and extra_min=0. Zero added minutes is supported despite rounded distance changing.',
    'repeat_stop': 'Snapshot confirms already_present=true, extra_min=0, Office destination and ETA 31; no duplicate stop was added.',
    'cancel_active': 'Correctly confirms cancellation of Cubbon Park navigation.',
    'cancel_idle': 'Snapshot has cancelled=null and no active route; no cancellation effect occurred.',
    'route_only': 'Correctly says route computed with ETA 13; does not claim navigation started. Route ID adds no requested information.',
    'independent_query': 'Both requested actions are represented: navigation and all three coffee results with the exact returned detours 1/31/67. Names omit location qualifiers and route ETA 13 is omitted. Detours belong to the read snapshot at navigation_version=0, route_id=null, not to the newly started route; text does not explicitly misattribute them.',
}

reviewed = []
for row in rows:
    key = (row['block'], row['arm'], row['case'])
    case = cases[row['case']]
    receipt_rows = request_map.get(key, [])
    expected_results = [{k: out[k] for k in ('tool', 'args', 'status', 'result', 'error')} for out in case['execution']['outcomes'].values()]
    payload_checks = []
    for receipt in receipt_rows:
        prefix, payload = receipt['user'].split('\nResults: ', 1)
        payload_checks.append(prefix == 'User request: ' + case['request'] and json.loads(payload) == expected_results
                              and receipt['status'] == 'completed' and receipt['response'] == row['text'])
    issues, omissions, nuances = [], [], []
    text = row['text']
    route_ids = sorted(set(re.findall(r'\bR\d+\b', text)))
    if route_ids:
        issues.append('unrequested_route_id')
    if row['arm'] == 'A':
        if row['case'] == 'replace':
            omissions += ['eta_min=40', 'replaced=Cubbon Park']
        elif row['case'] == 'already_active':
            issues.append('false_fresh_start_claim_despite_already_active')
            omissions += ['eta_min=29', 'preserved_stop=Ather Grid charger, Indiranagar']
        elif row['case'] == 'add_stop':
            omissions += ['eta_min=13', 'destination=MG Road Metro Station', 'extra_min=0']
            nuances.append('Stop locality Indiranagar is omitted, but the unique returned first result is identifiable.')
        elif row['case'] == 'repeat_stop':
            omissions += ['eta_min=31', 'destination=Office (Manyata Tech Park)']
        elif row['case'] == 'cancel_idle':
            nuances.append('Successful cancellation wording is imprecise for a no-op, but the next sentence explicitly says there was no active route; not classified as an unambiguous false effect.')
    if row['case'] == 'independent_query':
        omissions += ['eta_min=13']
        nuances.append('Both explicit requested outputs are present despite omitted supplementary ETA and shortened place names.')
    reviewed.append({**row, 'request': case['request'], 'actual_path': 'model' if row['logical_model_calls'] else 'direct',
                     'word_count_whitespace': len(text.split()), 'route_ids_spoken': route_ids,
                     'model_receipt_count_matches': len(receipt_rows) == row['logical_model_calls'],
                     'full_frozen_request_results_and_response_match_receipts': all(payload_checks),
                     'manual_review': {'note': review_notes[row['case']], 'issues': issues,
                                       'omitted_result_context': omissions, 'nuances': nuances,
                                       'unambiguous_false_effect_claim': 'false_fresh_start_claim_despite_already_active' in issues}})

arms = {}
for arm in ('A', 'B'):
    group = [row for row in rows if row['arm'] == arm]
    arms[arm] = {'all_ten_cases': metrics(group),
                 'primary_original_eight_cases_including_via_fallback': metrics([row for row in group if row['case'] in original]),
                 'successful_direct_only_descriptive': metrics([row for row in group if row['logical_model_calls'] == 0]),
                 'all_actual_model_fallbacks': metrics([row for row in group if row['logical_model_calls'] > 0]),
                 'original_two_model_expected_cases': metrics([row for row in group if row['case'] not in original]),
                 'per_case': {case: metrics([row for row in group if row['case'] == case]) for case in protocol['case_order']}}
blocks = [{'block': block, 'arm': arm, 'all_ten_cases': metrics([row for row in rows if row['block'] == block]),
           'primary_original_eight_cases': metrics([row for row in rows if row['block'] == block and row['case'] in original]),
           'case_rows': [row for row in reviewed if row['block'] == block]}
          for block, arm in enumerate(protocol['arm_order'], 1)]
verification = {name: {'expected': value, 'actual': sha(ROOT / name)} for name, value in protocol['frozen_sha256'].items()}
for value in verification.values():
    value['match'] = value['expected'] == value['actual']
report = {
    'created_at_utc': datetime.now(timezone.utc).isoformat(),
    'scope': 'All 40 frozen completed-execution summary replays; no tools, inference, tests or RTC logs accessed by this audit.',
    'metric_definition': 'Awaited summarize wall-clock milliseconds, not first token, speech, acoustic, dispatch or end-to-end latency. Percentiles use sorted[ceil(p*n)-1]; n16 p95 is the maximum, n2 p50 is the lower sample. Missing/error rows would remain denominators but have no completed timing.',
    'coverage': {'declared': 40, 'observed': len(rows), 'completed': sum(row['status'] == 'completed' for row in rows),
                 'exact_order_and_case_coverage': actual == expected, 'original_direct_case_count': 8,
                 'candidate_actual_direct_distinct_cases': sorted({row['case'] for row in rows if row['arm'] == 'B' and not row['logical_model_calls']}),
                 'candidate_original_direct_rows': 16, 'candidate_actual_direct_rows': 14,
                 'candidate_expected_logical_calls_from_original_coverage': 4,
                 'candidate_observed_logical_calls': 6, 'baseline_observed_logical_calls': 20,
                 'unexpected_candidate_fallback_case': 'via', 'model_receipts': len(requests),
                 'logical_calls_are_not_http_attempt_counts': True},
    'arms': arms, 'blocks': blocks,
    'all_fallback_rows': [row for row in reviewed if row['actual_path'] == 'model'],
    'semantic_review_counts': {arm: {'rows': sum(row['arm'] == arm for row in reviewed),
        'unambiguous_false_effect_claim_rows': sum(row['manual_review']['unambiguous_false_effect_claim'] for row in reviewed if row['arm'] == arm),
        'unrequested_route_id_rows': sum(bool(row['route_ids_spoken']) for row in reviewed if row['arm'] == arm),
        'rows_with_omitted_result_context': sum(bool(row['manual_review']['omitted_result_context']) for row in reviewed if row['arm'] == arm),
        'rows_over_30_whitespace_words': sum(row['word_count_whitespace'] > 30 for row in reviewed if row['arm'] == arm)} for arm in ('A', 'B')},
    'semantic_scoring_note': 'Omitted result context is recorded separately from false claims and omitted explicit requests. The frozen input expectation asks to preserve ETA, replacement identity and no-op state; not every omitted supplementary fact is independently a failure to answer the user. There is no blanket 40/40 quality-pass claim. All14 candidate direct texts are grounded and retain their required action context; candidate fallback still has route-ID/specificity weaknesses.',
    'verification': {'protocol_embedded_matches_file': comparison['protocol'] == protocol,
        'protocol_sha256_matches': comparison['protocol_sha256'] == sha(BASE / 'summary-protocol.json'),
        'frozen_source_and_input_hashes': verification,
        'all_current_frozen_source_and_input_hashes_match': all(value['match'] for value in verification.values()),
        'all_receipt_counts_match': all(row['model_receipt_count_matches'] for row in reviewed),
        'all26_model_receipts_preserve_full_frozen_request_results_and_output': len(requests) == 26 and all(row['full_frozen_request_results_and_response_match_receipts'] for row in reviewed),
        'model_hash_receipt': comparison['model_bytes_unchanged'],
        'model_hash_audit_limit': 'Measurement code verifies model files before and after its run; this audit does not reread large model weights while a separate RTC experiment is active.',
        'server_exit_receipt': comparison['server_exit']},
    'artifact_sha256': {name: sha(BASE / name) for name in ['summary-comparison.json', 'summary-protocol.json', 'inputs.json', 'summary-model-requests.jsonl', 'protocol-clarifications.json', 'responder-before.py', 'responder-candidate.py', 'analyze_summaries.py']},
    'limits': protocol['limits'] + [
        'This is ABBA with one shared server and ten fixed development inputs, two observations per case/arm, not independent samples or a held-out accuracy result.',
        'The first cold A/start row is retained (4677.86320799496 ms); block4 A/start is 2692.0657500013476 ms. Cache/order and unisolated host effects prevent a general latency claim.',
        'The original eight-case subgroup includes both slower via fallback rows. Fast-path-only numbers describe seven-case coverage and must not replace the primary denominator.',
        'All fallback outputs match across arms/blocks for the same case; candidate guarding reduces coverage without adding model fallback text regressions in these inputs.',
        'Historical test and protocol revisions, including misleading before filename and collection errors, are retained and explained in protocol-clarifications.json.',
        'The final candidate uses a positive complete-command guard. Unfamiliar phrasing, additional information requests and unsupported snapshots remain model fallback; these40rows do not exhaust that boundary.',
        'No audio or live tool effects occur in the timed comparison. Frozen result replay cannot demonstrate causal conversation correctness, safe current-state freshness or acoustic latency.',
    ],
}
with (BASE / 'summary-analysis.json').open('x') as stream:
    json.dump(report, stream, indent=2)
    stream.write('\n')
print(json.dumps({'coverage': report['coverage'], 'semantic_counts': report['semantic_review_counts'],
                  'hashes_match': report['verification']['all_current_frozen_source_and_input_hashes_match'],
                  'receipt_checks': report['verification']['all26_model_receipts_preserve_full_frozen_request_results_and_output']}, indent=2))
