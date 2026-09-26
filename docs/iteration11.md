# Keep trace-file I/O off the audio loop

Iteration 10 recorded a 436 ms event-loop stall at `trace_f.flush()` and a 113 ms stall at trace-directory creation. It also recorded a destination correction that reached voice detection but produced no transcript. This iteration fixes the directly observed file-I/O boundary and investigates the recognition gap separately. It does not assume one caused the other.

## Owned trace writer

Each room now owns a single-worker trace writer. The event callback freezes its JSON representation before another sink or caller can mutate it, then submits the line without waiting for disk. Directory creation, opening, ordered writes/flushes and closing run on that worker. This preserves trace format and per-room submission order while removing those file operations from the audio loop.

Submission is no longer an immediate persistence acknowledgment. Normal room cleanup queues close after accepted events and awaits completion. A file failure or rejected event makes cleanup incomplete and is logged; uncertain partial writes are not replayed. Serialization failures are counted too, because the event bus otherwise catches sink exceptions. An unsuccessful trace drain does not produce a successful room-cleanup log.

The close wait has a deadline and does not cancel or hide a still-running native file operation. A subsequent close can observe its eventual completion. A native call may still delay process exit; no bounded native shutdown is claimed. `flush()` is not `fsync()`, so successful cleanup is not a power-loss durability guarantee. Pending trace work can grow during sustained slow I/O, just as event history remains unbounded; this change is not a memory-capacity solution.

Twelve new tests cover slow directory/open/write/flush/close boundaries, ordered immutable event snapshots, partial-write and flush failures, observable serialization rejection, timeout/cancellation of the close waiter, eventual drain and independent rooms. The existing six room-lifecycle tests also pass with the real writer. **The complete suite passes 555 tests in 5.39 seconds.** An initial test-fixture failure intercepted a read as a write; its log is retained, and the corrected final focused run records unchanged source/test hashes.

## Controlled file-delay comparison

The protocol declares thirty trials: open, first flush and close, five A/B pairs each. A reproduces the previous synchronous sink semantics from the saved entrypoint; B uses the production writer. Exactly one 100 ms file delay is injected per trial. Every trial submits two fixed events and checks their complete ordered JSONL after close. No models or RTC services run.

| Injected operation | Synchronous median worst ticker delay | Candidate median worst ticker delay |
| --- | ---: | ---: |
| Open | 102.951 ms | 0.339 ms |
| First flush | 105.917 ms | 0.554 ms |
| Close | 104.717 ms | 0.339 ms |

All thirty trials preserve both events and acknowledge close. The candidate's largest individual ticker delay is 2.639 ms and remains in the evidence. The metric is maximum observed overshoot of a repeating 1 ms sleep, including brief pre/post shoulders. Its schedule restarts after each wake; it does not count every missed deadline or measure accumulated lateness. Fixed A/B ordering and other applications limit generalization. This is a controlled component result, not a live-conversation latency improvement.

The [raw comparison](../results/iteration11/trace-io-comparison/report.json), [declared protocol](../results/iteration11/trace-io-comparison/protocol.json) and [independent analysis](../results/iteration11/trace-io-analysis.json) retain every row and source/file checksum.

## Recognition gap and echo warmup

An [installed-SDK audit](../results/iteration11/recognition-boundary-audit.json) identifies a different boundary. LiveKit Agents 1.8.3 defaults to a three-second acoustic-echo-cancellation warmup. While that timer is active and the agent is speaking, its activity layer substitutes silence for STT input while the voice detector continues receiving the original audio. It also suppresses tentative interruption during warmup. The configured interruption word threshold is zero; a word-count threshold does not explain this case.

The [component reproduction](../results/iteration11/aec-boundary-reproduction.json) calls the installed SDK methods with dummy channels and a real three-second timer. STT receives the original nonzero PCM before and after the timer, but zeros during it; VAD receives original PCM in all three observations. No recognition model, VAD inference, network or full session runs in this reproduction.

Iteration 10's failed correction falls entirely within the predicted warmup window following that room's first observed agent-speaking state. This is a source-and-timing inference because the original gate state was not recorded. It is not proof that the same routing occurred in that historical capture. `AgentSession(aec_warmup_duration=...)` controls this behavior separately from turn handling; no production setting changes here. Disabling suppression for echo-free injected audio needs a separately declared comparison and does not establish safety for a real microphone.

## Live verification

The live protocol repeats the exact four waypoint/baseline/waypoint/correction conversations and their existing WAVs, with recognition, model, timing and interruption settings unchanged. An experiment-only worker records the SDK's actual STT-frame substitution intervals in memory and writes them after the worker returns. It passes frames to the original method unchanged. The instrumentation has overhead, so this is an integration/diagnostic run rather than an isolated end-to-end latency comparison. A killed worker may not save the buffered diagnostic report; missing evidence must remain missing.

All four captures complete, retaining **19 successful attempts and all seven expected effects**. Both waypoint requests add the declared coffee stop on the unchanged MG Road route, and the airport-to-office correction completes. Whole-stream ASR retains all four final requested replies, including both 13-minute/zero-extra stop results, airport 88 and office-replacement 29. Number word spellings and proper-name deviations remain verbatim in the [guarded whole-stream review](../results/iteration11/received-audio-review/qualitative-review.json); this is a content proxy, not human listening.

Interruption remains imperfect. Waypoint 1 and the destination correction stop the original result; last old non-silent PCM arrives 680.696 and 737.756 ms after follow-up onset. Waypoint 2's original result finishes instead, with old non-silent PCM continuing to 9.580 seconds after onset. Its 2.58-second segment takes 13.603 seconds in the recognition-stage timer, and its transcript arrives 14.383 seconds after scheduled speech end. Exact VAD-segment PCM and internal queue/native timing were not retained, so matching rounded segment duration does not prove identical recognition input or identify the cause. The two successful interruptions do not erase this failed check.

| Speech-end to client observation | Initial requests, n=4, p50/p95 | Follow-ups, n=3, p50/p95 |
| --- | ---: | ---: |
| Acknowledgment PCM | 8.261 / 10.972 s | 4.080 / 16.300 s |
| Mutation-success receipt | 14.597 / 16.857 s | 10.954 / 18.959 s |
| Substantive PCM | 16.683 / 17.904 s | 12.616 / 20.859 s |

Every declared turn remains in these nearest-rank summaries. Four initial turns include a slow first plan; all follow-ups include the slow second waypoint. Final follow-up handles finish at 20.370 seconds median / 28.849 seconds maximum after speech end. Two deliberately interrupted initial handles have null completion values. Received PCM and handle state are distinct observations, neither a physical speaker measurement. Historical run-to-run differences do not establish an isolated trace-offload speedup.

All four trace writers drain successfully: **194 accepted, 194 flushed, zero rejected**, with their JSONL line counts checked separately. All application cleanup callbacks finish, all agents depart, and the worker exits 0 without escalation, 1.146 seconds after its drain log. Client/server event equality is 3/4; the destination-correction capture misses initial listening. The failed full-trace check remains. Two clients have no disconnected callback in their event files despite successful departure; their disconnect-relative cleanup timing stays unavailable. All four server jobs receive `JS_FAILED` labels on departure, separately from application cleanup. There are now 46 actual RTC captures across seven frozen iterations.

The diagnostic worker records 18 substituted frames per room, 21,600 samples at 24 kHz (0.9 seconds). Every substituted frame is observed while AEC warmup is active; the first substituted frame in each interval is verified zero. These intervals occur during the initial acknowledgment, before follow-up input. **No follow-up is discarded by that gate in this run.** Thus the earlier missing-correction inference gains a demonstrated mechanism but is not reproduced live here. Diagnostic observation reports zero errors and unchanged source hashes; aggregate observer work is 32.646 ms, with a 0.293 ms largest per-frame observation. State reads, clocks and its observer lock still change runtime overhead.

The worker retains two unsampled watchdog warnings (364 and 123 ms), four native-handle warnings and three phonemizer warnings. The [supplementary review](../results/iteration11/supplementary-log-review.json) preserves all 33 runtime warnings (worker 10, LLM 1, LiveKit 18, recorders 4), including the development-key warning. There are no runtime error/traceback rows in those logs; the separate preparation failure below remains. Waypoint 2's agent state resumes speaking 2000.565 ms after the user-listening state, consistent with the default two-second false-interruption timeout; explicit pause/resume events were not recorded, so this is an inference. The 344 memory samples show a 0.437 GB minimum system available, swap 4.716 GB initially / 8.862 GB finally / 9.592 GB maximum, and LLM/worker RSS peaks 4.851/0.888 GB. One final process-sampling error remains. These samples do not establish process capacity or a cause for slow recognition.

A preparation helper failed with `KeyError('baseline')` before enriching the protocol, then the shell continued into the already-declared runner. That error is preserved in `preparation-error.json`; the running suite was neither restarted nor excluded. The tested-source check had passed, and the runner independently froze actual production, protocol, wrapper and input bytes and verified them unchanged. The separate whole-stream review-method file was written after RTC launch but before ASR; all cases/content criteria were already declared and none changed. Fresh pre-RTC full model hashing was not completed. [Post-inference full hashes](../results/iteration11/post-inference-validation.json) match the earlier model reference, with this timing limitation stated rather than retroactively inventing a preflight check.

## Decision and next checks

Keep the trace writer: focused tests, controlled file-delay evidence and all 194 live trace events support its intended boundary. The live latency differences do not isolate its effect. [Final validation](../results/iteration11/final-validation.json) records source checks, artifact hashes and delivery status.

The next diagnostic priority is to separate STT queue wait, native inference and result delivery while retaining exact segment identity; the 13.603-second stage measurement cannot do that. Next, compare the SDK warmup setting on explicitly echo-free injected input with criteria frozen in advance. Real microphone/echo acceptance and server failed-job investigation remain separate from both experiments.

The delivery check also found that the generic raw-output ignore rule hid the referenced iteration 9/10 speech evidence. Narrow exceptions now expose those two frozen synthetic-audio directories for user-selected staging. Earlier iteration 2 raw outputs already have visible archives and hash manifests. No files were staged.

## Reproduction

Saved experiment folders refuse overwrite. Copy to a fresh directory and redeclare changed paths/hashes for a new run rather than editing historical receipts.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python results/iteration11/measure_trace_io.py
.venv/bin/python results/iteration11/reproduce_aec_boundary.py
.venv/bin/python results/iteration11/run_suite.py
.venv/bin/python results/iteration11/audit_live.py
.venv/bin/python results/iteration11/run_received_audio_supervised.py
```

The PR still requires user-selected staging. Organizer Theme 05 access, human microphone/echo behavior, physical playback timing, live cancellation/repeat visits, simultaneous rooms and fresh adapted FDB evaluation remain separate requirements. This file-I/O change does not establish complete full-duplex reliability.
