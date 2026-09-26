# Locate delayed recognition and server job-status boundaries

Iteration 11 completed all seven route effects, but one waypoint input took 13.603 seconds in the recognition stage and arrived too late to stop the original reply. The existing timer includes shared-worker waiting, WAV writing, file decoding, recognition and asynchronous return. Exact VAD-segment bytes were not retained, so matching rounded durations did not establish identical input.

This iteration repeats the declared four development conversations with unchanged production code, model settings, input WAVs and speech policy. Experiment-only wrappers will preserve original calls while separating those timing boundaries and retaining normalized synthetic segment bytes. No extra accelerator synchronization is inserted: front-end and generation timings remain host-call boundaries, with deferred computation and other workloads explicitly limiting interpretation.

The [declared plan](../results/iteration12/plan.json) and [four-case protocol](../results/iteration12/protocol.json) preserve every capture, attempt, effect, failed interruption and cleanup outcome. The whole received stream from every case is reviewed after the timed services stop, without clipping or success-based selection. The [review method](../results/iteration12/received-audio-review-protocol.json) is frozen before launch.

Six focused diagnostic tests pass in 1.10 seconds, covering PCM/stage/room/context fidelity, keyword arguments, running and queued cancellation, original exceptions and bounded retention. Production code is unchanged from the 555-test checkpoint. Before launch, current sources, inputs, review method and ten model reference files were verified. The launcher now requires that successful preflight and rejects changed files before creating its stack; three negative guard checks cover missing, failed and stale preparation. Post-inference hashes still match.

## Recognition-stage result

The [stage analysis](../results/iteration12/stt-stage-analysis.json), independently checked by the [stage audit](../results/iteration12/stt-stage-independent-audit.json), retains seven engine calls: one silent warmup and six user-recognition calls. The failed baseline sent no input and therefore produced no live recognition call. All seven submitted PCM snapshots match the exact bytes written to their WAVs. The seven inputs have distinct hashes; even the two repeated waypoint conversations produce slightly different VAD segments (initial 2.816/2.848 seconds, follow-up 2.580/2.548 seconds).

| User recognition call | Total engine API time | Submit to worker entry | File decoder | Parakeet generation |
| --- | ---: | ---: | ---: | ---: |
| Waypoint 1 initial | 0.907 s | 0.081 ms | 87.597 ms | 0.815 s |
| Waypoint 1 follow-up | 0.926 s | 0.126 ms | 83.694 ms | 0.840 s |
| Waypoint 2 initial | 6.853 s | 0.092 ms | 73.092 ms | 6.773 s |
| Waypoint 2 follow-up | 1.281 s | 0.078 ms | 88.659 ms | 1.190 s |
| Correction initial | 1.244 s | 0.119 ms | 75.534 ms | 1.164 s |
| Correction follow-up | 1.427 s | 0.118 ms | 333.742 ms | 1.079 s |

The slow call is dominated by the generation boundary. Queue waiting, WAV operations and file decoding do not explain its multi-second duration. This does not identify the cause inside generation, establish exclusive accelerator time, or explain the earlier uninstrumented 13.603-second call. Different segment bytes, compilation/cache state, memory pressure and other workloads remain possible factors. Removing FFmpeg could reduce a smaller measured cost, but would not address this observed dominant delay.

All native futures finish. There are no dropped records, retention overflows or observation errors; 1,083,648 PCM bytes are retained. Measured helper overhead totals 3.143 ms, with a 0.546 ms largest hook; some dispatch, ContextVar and wrapper overhead is outside that counter. Nested model and child-stage durations must not be added together. No production optimization is selected from this diagnostic run.

## Conversation and recording outcomes

All four RTC attempts remain in the [corrected audit](../results/iteration12/audit-v2.json): **3/4 completed captures, 16/19 planned attempts and 6/7 expected route effects**. Both stop additions and the office replacement complete. All three original results receive interruption requests; their last old non-silent PCM arrives 679.859, 659.704 and 859.006 ms after follow-up onset. The complete [received-audio review](../results/iteration12/received-audio-review/qualitative-review.json) runs ASR on all four streams. Both stop replies preserve 13 minutes/zero extra and the correction preserves replacement plus 29 minutes, with name deviations retained. The baseline's 44.36-second stream has no above-threshold activity and yields an empty transcript.

The [readiness audit](../results/iteration12/readiness-boundary-audit.json) shows that the baseline fails while waiting for session readiness, before any request is published. Its raw packet log contains initial listening and `session_started`, matching the server trace, but public SDK packets expose no participant. The recorder rejects them and never becomes ready. An agent audio track subscribes 24.773 ms later. This is an attribution/readiness failure, not proof those packets were lost in transport or that recognition missed a spoken request. SDK 1.1.18 discards the transport identity when participant lookup fails, so the saved public packets cannot distinguish a blank original identity from a lookup race. They must not be assigned to a later agent by inference.

Promoted client/server trace equality is 3/4; decoded raw packet content equals the server event list in all four. The latter diagnostic does not override the failed trusted-event check. The original audit encountered `KeyError('timing')` on the prepared but unpublished baseline clip; [that audit](../results/iteration12/audit.json) is preserved. The new derivation records missing timing as null and `input_published_1=false`, retaining the baseline in every applicable check denominator. The three completed cases' prior metrics are unchanged.

| Scheduled speech-end to client receipt | Initial requests, n=3/4, p50/p95 | Follow-ups, n=3/3, p50/p95 |
| --- | ---: | ---: |
| Acknowledgment PCM | 3.207 / 9.626 s | 3.419 / 4.019 s |
| Mutation-success receipt | 13.989 / 16.326 s | 6.195 / 11.138 s |
| Substantive PCM | 15.047 / 17.376 s | 7.120 / 12.780 s |

The [latency summary](../results/iteration12/latency-summary-v2.json) keeps the missing baseline and all seven declared turns. Three initial results are intentionally interrupted; the baseline never starts, so no initial completion time is available. Follow-up completion is 15.112 seconds median / 20.511 seconds maximum. These are client observations relative to scheduled input sample timing, not physical playback or a controlled improvement. The correction publisher finishes draining 420.046 ms after its scheduled end, which limits endpoint interpretation.

All four room cleanup callbacks finish, all agents depart, and the worker exits 0 without escalation 1.159 seconds after the drain log. All 166 accepted trace events are flushed, with no rejection. Four server `JS_FAILED` statuses remain separately recorded. Two clients lack disconnected callbacks, so their disconnect-relative cleanup times remain unavailable.

The [supplementary review](../results/iteration12/supplementary-log-review.json) retains 31 warning rows and no runtime error/traceback log rows; the readiness timeout traceback is retained in the baseline report. Three watchdogs last 105.1, 118.9 and 101.0 ms. Two sampled stacks show SSL-context construction during client initialization or SDK teardown; one is unsampled. The 348 memory observations reach 0.326 GB minimum system available and swap 3.844 GB initially / 8.919 GB finally / 9.564 GB maximum. Two final process-sampling errors remain. These samples do not identify the cause of generation delay.

AEC observation records 54 substituted frames across the three speaking rooms, totaling 2.7 seconds, with none overlapping follow-up input. The silent baseline does not exercise speaking warmup. No AEC or interruption setting is changed.

## Why cleanup and server status disagree

The [job-status boundary audit](../results/iteration12/job-status-boundary-audit.json) traces the installed SDK and matching server tag. LiveKit Agents 1.8.3 disconnects the room before awaiting application shutdown callbacks. Its thread executor marks success after the job thread joins, and the worker then queues the terminal update. LiveKit server 1.13.7 maps agent departure to `JS_FAILED` and removes that active job; a later success cannot replace it. The server removal runs asynchronously, so this source path does not imply every graceful shutdown must fail. [Server termination implementation](https://github.com/livekit/livekit/blob/v1.13.7/pkg/agent/worker.go#L445).

All four iteration 11 server failures precede application cleanup start; cleanup finishes 2.203–5.682 ms after the failure record. The logs do not capture a subsequent per-job success message or arrival. The installed binary's Homebrew version selects the source tag; binary-to-source reproducibility is not claimed.

Two [installed-method reproductions](../results/iteration12/job-status-order-reproduction.json) use dummy room/channels and a resolved join future, with no server, network, model or actual worker thread. Both preserve the disconnect-before-callback order. One callback deliberately raises; the SDK logs that error yet later maps thread completion to success. Thus neither a server failure label nor an SDK success message alone proves application cleanup.

Keep the current session-close-to-job-shutdown bridge and separate outcome receipts. An application workaround that announces success early would hide unfinished or failed cleanup. A coherent upstream solution needs acknowledged graceful-completion ordering after required cleanup, while preserving failure for unexpected departure. No upstream issue has been posted and no SDK/server fix is claimed.

## Decision and next steps

Retain the experiment and source evidence. The next measurement improvement is to preserve native sender identity before the SDK's participant lookup, buffer unresolved packets, and accept them only when that same identity resolves to an agent. Blank or non-agent identities must remain rejected. Test the ordering and attribution boundary before repeating a fresh capture; do not repair the failed historical result.

The recognition investigation should next use the exact retained segments to distinguish generation-state and resource-pressure hypotheses. Any optimization requires a new controlled comparison of content, interruption behavior and latency. Human microphone/echo acceptance, physical playback, live cancellation/repeat visits, concurrent rooms, adapted FDB evaluation and organizer Theme 05 verification remain separate requirements.

All 39 source files from the preflight have an exact [source snapshot](../results/iteration12/source-snapshot.json) so later edits do not erase the evaluated implementation. Restore snapshot files to their recorded original paths when reconstructing the run; model/runtime receipts remain separate.

Saved output folders refuse overwrite. Source and protocol checks for this checkpoint are in [final validation](../results/iteration12/final-validation.json). No files have been staged and no PR has been created.
