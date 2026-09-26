# Preserve requested details and spell out arrival estimates

Iteration 8's four received-audio transcripts rendered “ETA” as “Ida.” Its direct-summary guard also depended on a resolver rewrite, which could omit an extra question before the responder saw it. This iteration addresses those two observed boundaries without changing planning, tools, commitment or interruption policy.

## Original request and repaired interpretation

Five scripted coordinator/responder integration cases use real simulated navigation outcomes and a recording model stub. Before the change, three fail: an abbreviated distance request and a nearby-place-list request bypass synthesis entirely, while an unfamiliar repaired request reaches synthesis without the original distance question. Empty-repair and pure-command controls pass. These are reproduced handoff defects, not inferred model behavior.

The coordinator now passes the complete utterance it supplied to the resolver as the summary request. A nonempty repair is separate interpretation context. Direct-summary eligibility uses the complete request; fallback receives both texts and the actual tool outcomes. Its instructions retain original questions while respecting corrections. A recognized pure command still uses direct result formatting. No-op, unknown-outcome and thirty-word fallback behavior remain intact.

All five new cases pass. One earlier injected-responder test expected repaired text to replace the original; its stub and assertion were updated to the new two-text contract. The intermediate failure is retained. The final focused run passes **40 tests**, and the full suite **543 tests**, before inference. Existing two-argument callers remain valid; custom responder implementations receiving a nonempty repair now need to accept the optional `repaired_request` keyword.

This preserves information at the coordinator/responder handoff. Whole-span ASR re-transcription may already have changed the utterance, a planner may still omit calls, and retaining a question in the prompt does not establish that the model answers it. Detailed model-answer fidelity needs separate evaluation.

## Arrival-estimate speech comparison

Direct route confirmations now say “Estimated arrival in 88 minutes”; stop confirmations use “Estimated time to …”. The generated number still comes from the successful result. These changes add one or two words. Summaries exceeding thirty words retain model fallback rather than truncating facts.

The protocol freezes four previously observed development replies: airport start, replacement with the office route, added coffee stop and already-present stop. The two earlier RTC texts and two frozen summary texts are preserved with source hashes. An untimed real-tool/result-replay check verifies that both responder versions generate the declared strings. This uses known development cases to investigate a known issue, not held-out data.

The offline experiment renders and transcribes four texts in A/B/B/A blocks: **sixteen declared/completed trials**, eight per wording, with pinned local Kokoro and Parakeet. It keeps the first cold inference, every full PCM/WAV, verbatim transcript, failure and timing. Models/source bytes are checked before and after; both speech pools close and the supervised process exits 0.

Every acronym reply is transcribed as “EDA,” “Ida” or “Eda.” All eight spelled-out replies retain the expected estimate phrase. However, **both versions transcribe the added-stop duration of 13 minutes as 30 minutes, twice each**. The expected duration value is retained in six of eight replies per wording. The zero-extra-minutes fact survives, and destination-change/no-op relationships remain represented with proper-name spelling differences. The wording change addresses the observed estimate-label ambiguity; it does not resolve the duration error or demonstrate general intelligibility.

The same production ASR judges synthetic output from one voice. It cannot separate pronunciation from recognition errors, and no human listening was performed. Repeated same-case TTS PCM is identical within each arm, so these repetitions are not independent speech samples. Whole-stream resampling differs by at most two PCM16 sample units between repeats; the original files remain preserved. The added words increase synthesized duration by **0.619–0.960 seconds** across the four cases. This is a real cost of clearer estimate wording, not a latency improvement.

## Live audio verification

The four-case local RTC protocol reuses the exact earlier airport/correction WAVs, one stack and serial fresh rooms. It checks actions, result speech, interruption, traces and cleanup separately. The live experiment is a fresh integration check; iteration 8 is historical context, not a controlled latency baseline. Raw evidence and the whole-stream review method are under `results/iteration9/`.

All four captures complete, retaining **18 successful attempts and six expected effects** without stale-result or tool errors. Both old airport results are interrupted and both office replies finish. Full client/server coordinator-event equality holds for **2/4**; the other two miss only initial listening. Their exact event payloads are present in raw data packets with no participant identity. The recorder requires an identified agent before promoting a packet to its coordinator-event stream. These failed equality checks remain; this observation does not establish transport loss.

| Speech-end to observation | Initial requests, n=4, p50/p95 | Corrections, n=2, p50/p95 |
| --- | ---: | ---: |
| Acknowledgment PCM | 3.335 / 16.815 s | 3.579 / 3.640 s |
| Navigation-success receipt | 8.065 / 20.868 s | 9.141 / 10.494 s |
| Substantive PCM | 9.019 / 21.994 s | 10.500 / 12.078 s |

Percentiles use nearest rank and retain every declared turn, including the slow first plan and a slow third recognition. The latter receives a transcript 14.843 seconds after speech end. The worker records processing 1.92 seconds of audio in **14.195 seconds**, between listening and finalized-transcript events. That timer includes worker queueing, native recognition, temporary-file I/O and return scheduling. The available logs locate a slow recognition-stage interval but do not identify its cause.

Across all six turns, tool-success receipt to result-text receipt p50/p95 is 2.628/4.458 ms; to substantive PCM it is 1.073/1.584 seconds. The two corrected replies finish at 18.256/19.831 seconds after input speech end. Intentionally interrupted initial handles retain null completion values. The final non-silent old PCM arrives 738.239/759.912 ms after correction onset, including untagged tail. These are received-data/audio observations, not physical audible-stop or population latency estimates.

All four application cleanup sequences finish and agents depart; the worker exits 0, 1.290104 seconds after its drain log, without escalation. However, LiveKit labels all four server jobs `JS_FAILED` with “agent worker left the room” during teardown. The same labeling exists for all eight iteration 7 and four iteration 8 jobs. Completed effects, application cleanup and process exit therefore do not prove successful server job status. An [installed-SDK audit](../results/iteration9/job-status-ordering-audit.json) finds that room disconnect precedes application cleanup, with successful runner status queued later. The observed server failures precede cleanup by about 1.6–2.5 ms. This supports an ordering-mismatch hypothesis; actual outgoing status receipt and server transitions remain unobserved, so neither a harmless label nor a PRISM cleanup defect is established.

The worker has thirteen structured warnings: five native handle warnings, six phonemizer warnings and two startup watchdog intervals, 270.0/123.8 ms. The 270 ms warning samples `selectors.select`; the other has no stack. Neither coincides with the slow recognition. A separate development-key warning and other service/recorder warnings remain in the raw logs. Memory sampling records available system memory as low as 0.158 GB, swap 5.144 GB initially/8.980 GB finally/9.325 GB maximum, and peak LLM/worker RSS 4.861/1.163 GB. All 280 samples, including three final process-sampling errors, remain. Existing swap, other applications, shared pages and unsampled allocation prevent capacity or causal claims.

After owned services exit, Parakeet reviews all four whole received streams. Every transcript retains the spelled-out estimate and expected 88- or 29-minute value, with the requested destination/replacement relation. Proper names still differ. These airport/office cases do not exercise the separate added-stop 13-to-30 error. There are now **38 actual RTC captures across five frozen iterations**, not thirty-eight trials of this source.

## Reproduction and evidence

[Offline speech analysis](../results/iteration9/eta-analysis.json) retains every row, audio hash, duration mismatch and paired observation. Its helper checks the exact reviewed transcripts before attaching manual annotations, so a fresh changed transcript requires a fresh review. [Received-audio review](../results/iteration9/received-audio-review/report.json) and [qualitative notes](../results/iteration9/received-audio-review/qualitative-review.json) preserve all four whole RTC streams. [Timing analysis](../results/iteration9/latency-summary.json) and the [supplementary log census](../results/iteration9/supplementary-log-review.json) expose the recognition delay, trace omissions and all forty scoped warnings across worker, server and recorders. Tests, protocols, source snapshots, model receipts, supervision and cleanup records remain alongside them.

For an offline repeat in the same checkout/environment, preserve the originals and copy only the runners/protocol. Remap the two copied runner keys while verifying their bytes; the other frozen source/input paths deliberately still refer to retained originals:

```bash
mkdir results/iteration9-repeat
cp results/iteration9/{measure_eta_speech.py,run_eta_supervised.py,eta-protocol.json} results/iteration9-repeat/
.venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
folder = Path('results/iteration9-repeat')
path = folder / 'eta-protocol.json'
method = json.loads(path.read_text())
for name in ('measure_eta_speech.py', 'run_eta_supervised.py'):
    expected = method['frozen_sha256'].pop(f'results/iteration9/{name}')
    copied = folder / name
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == expected
    method['frozen_sha256'][str(copied)] = expected
method['repeat_of_protocol_sha256'] = hashlib.sha256(Path('results/iteration9/eta-protocol.json').read_bytes()).hexdigest()
path.write_text(json.dumps(method, indent=2) + '\n')
PY
.venv/bin/python results/iteration9-repeat/run_eta_supervised.py
```

This requires the recorded dependencies and exact local speech-model paths. The runner rejects changed frozen source/model bytes and existing output; it does not download models. Changed production code/configuration requires a separately declared experiment. Keep all sixteen rows, including the first cold sample and number errors. Do not describe the generated comparison as a planner or live-audio evaluation.

For RTC, follow the fresh-directory procedure in [iteration 7](iteration7.md), substituting `iteration9` as the source folder and using a distinct output directory. That runner depends on retained iteration 4 server/launch records. Inspect every owned process exit before offline received-ASR review. Use the four-case protocol and compare source hashes before claiming an unchanged repeat.

## Decision and next work

Retain original-request preservation and spelled-out estimate wording, with the limits above. The next experiments are ranked by the observed failures:

1. Investigate the 13-versus-30 speech error with controlled wording/pronunciation changes and preserved number-fidelity checks.
2. Isolate recognition latency under the local model workload, including queueing and memory pressure, rather than attributing the 14.195-second interval from correlation.
3. Measure actual model answers to requests containing both an action and a question; prompt preservation alone is insufficient.
4. Observe SDK/server job-status transitions during expected departure before changing cleanup order.
5. Handle initially unattributed data packets without inventing sender identity or discarding evidence from the trace.

Slow planning, short-command/empty-transcript interruption recovery, active-tool disconnects, simultaneous live rooms, microphone/echo and physical-speaker measurements, fresh adapted FDB evaluation and organizer Theme 05 verification also remain outstanding.
