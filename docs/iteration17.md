# Cache retention, useful-work timing and sender identity

**Decision: retain two cache entries.** The one-entry arm reduced the observed logical cache peak, but failed three of six required interruptions. Both two-entry arms passed all six interruption-request checks; passing that check does not establish prompt audible cutoff. All three arms completed their ten requested effects and four final replies. This is a failed candidate quality screen, not evidence that cache retention caused the failures.

Iteration16 completed the required navigation effects and final replies, but recorded substantial system swap and two missing trusted trace events. This iteration examines those two concerns separately. Production source, recorder behavior, models and audio remain fixed at iteration16. The existing 655-test receipt is inherited after checking all 76 tested source hashes; those tests are not described as newly run.

## Declared cache comparison

The [comparison protocol](../results/iteration17/comparison-protocol.json) specifies three fresh stacks in a two-entry, one-entry, two-entry order. Each stack runs the same four cancellation/return cases, for twelve captures, thirty input turns and effects, eighteen interruptions and twelve final replies overall. The same three WAVs, case order, deadlines, expected outcomes, model bytes, seed, prefill size, speech behavior and instrumentation apply throughout. The initial fresh-process request remains included. No received-speech ASR runs until all three timed stacks have exited.

Only the LLM server's `--prompt-cache-size` value changes. The nominal 256 MiB byte setting remains recorded, but the installed seeded sequential serving path does not enforce it. A smaller entry count may reduce logical retained KV data while increasing prefill work. Logical cache bytes, process RSS and whole-system available memory/swap measure different things; none alone proves reduced allocation or a latency cause.

All declared cases, failures, extra calls, repair work, anonymous packets and unfinished responses remain visible. Fulfilled-case time starts at the final input's scheduled speech end and requires every backend effect, all final spoken facts and a finished final reply. Trace equality and cleanup have their own outcomes. Exact normalized STT PCM comparisons remain separate from equality of the source WAVs.

The decision is a screening result, not adoption. A candidate quality failure rules out a favorable label. A promising result requires the one-entry behavior, a lower observed logical-cache peak than each baseline, complete candidate quality, and fulfilled-reply p50/p95 no worse than either baseline. Extra prefill work remains disclosed. One middle candidate process in fixed order cannot establish a causal memory benefit or general performance improvement; the two-entry production setting stays unchanged.

The [preparation check](../results/iteration17/preparation-check.json) and [independent review](../results/iteration17/prelaunch-review.json) verify identical methods, unchanged case requirements, the single argument change and pinned transport configuration. The [snapshot](../results/iteration17/source-snapshot.json) preserves 150 source, protocol, input and method files before the first model preflight. The independent review verdict preceded the first timed launch; its receipt was saved afterward. Every arm retains its own full-byte model/source preflight, actual launch arguments and process identities.

## What the identity evidence supports

The [installed-source investigation](../results/iteration17/identity-boundary-investigation.json) bounds iteration16's two anonymous packets. Their empty sender strings are already present in the native SDK event before Python participant lookup. The installed RTC event exposes no alternate sender SID or participant handle. A required protobuf string does not require a nonempty value, and the SDK documents empty identity for data from the server SDK. The retained recorder evidence does not distinguish an explicitly empty field from a missing required field read as its default.

Room handles, buffer handles, payload contents, topic, packet order and a later participant connection cannot authenticate these packets as agent messages. The recorder's rejection is therefore appropriate. Matching decoded content to the server trace shows preserved raw content; it cannot repair trusted attribution. No receiver trust rule, SDK code or frozen failure result changes here. Native/server origin remains unproved, and a future presence/kind observation would need to preserve that distinction rather than guessing a sender.

## Preserved outcomes and failures

The [independent RTC audit](../results/iteration17/rtc-audit-review.json) retains all twelve captures, thirty exact final input transcripts, thirty requested effects and seventy successful physical attempts. Four additional static-search reuses have provenance. Both two-entry arms repair one compute-only plan on the first return-to-airport turn before dispatch; the one-entry arm needs no repair. All thirty accepted plans and both rejected parsed plans remain recorded. There is no fallback or semantic invariant failure in these cases.

Whole-stream local ASR review finds the required final facts in all twelve replies: six airport cancellations and six airport returns that explicitly replace the office and give an 88-minute estimate. This is an ASR proxy, not human listening. Original transcripts preserve proper-name variants such as “Kempegauda,” “Kempigauda” and “Maniata,” and the “canceled” spelling. Final speech/effect completion does not erase interruption or attribution failures.

The one-entry arm's first cancellation, first interruption in the second return case, and second cancellation let the old result handle finish. The corresponding new final transcript arrived 14.440, 22.164 and 17.014 seconds after followup onset, although user-speaking receipts arrived at 223, 287 and 237 ms. Old PCM tails were 8.499, 9.180 and 8.559 seconds. These are measured receipt boundaries; they do not prove the responsible scheduling or memory mechanism. The second interruption of the second return case did pass, but its old PCM tail still reached 7.875 seconds.

Trusted trace equality passes 2/4, 3/4 and 3/4 cases in arm order. Four initial listening packets have empty native sender identity and remain rejected. Raw decoded/server equality passes all twelve; that comparison cannot authenticate the sender. All 806 accepted server trace events flushed. The recorder buffered no packets. The twelve server `JS_FAILED` labels remain visible despite successful application cleanup.

The logs retain 116 warning rows, three watchdog observations, two slow-Silero warnings in the one-entry arm, and one LLM broken-pipe traceback while writing SSE `[DONE]` in the final two-entry arm. These counts are not mutually exclusive categories. All workers exit 0, LLM services exit after their requested termination signal, and LiveKit services exit 0, without escalation. All twenty-one suite PIDs and three later audio-review child PIDs are absent at the post-inference check.

## Timing and retained cache

The [comparison report](../results/iteration17/comparison-report.json) preserves every case and turn, with no evidence consistency issue. Values below use seconds and nearest-rank p50/p95; each arm has four initial turns, six followups, four final replies and six interruption pairs. Fresh-process first requests remain included. Scheduled input speech end, client PCM arrival and handle state are receipt boundaries, not physical audio timing.

| Outcome | Two entries, first | One entry | Two entries, return |
| --- | ---: | ---: | ---: |
| Requested effects | 10/10 | 10/10 | 10/10 |
| Complete final speech facts | 4/4 | 4/4 | 4/4 |
| Required old handles interrupted | 6/6 | 3/6 | 6/6 |
| Trusted full-trace equality | 2/4 | 3/4 | 3/4 |
| Cases passing every core check | 2/4 | 0/4 | 3/4 |
| Initial acknowledgment PCM p50/p95 | 3.161 / 5.281 | 8.621 / 20.980 | 5.297 / 7.303 |
| Initial substantive PCM p50/p95 | 10.820 / 17.246 | 18.342 / 26.400 | 12.663 / 17.757 |
| Initial mutation success p50/p95 | 9.564 / 16.112 | 17.160 / 25.243 | 11.509 / 16.427 |
| Followup acknowledgment PCM p50/p95 | 3.240 / 3.419 | 8.341 / 22.239 | 3.600 / 5.238 |
| Followup substantive PCM p50/p95 | 9.539 / 14.580 | 17.460 / 30.279 | 9.619 / 15.300 |
| Followup mutation success p50/p95 | 8.053 / 13.032 | 15.650 / 28.690 | 8.133 / 13.587 |
| Fulfilled final reply p50/p95, 4/4 | 9.689 / 22.281 | 21.385 / 25.166 | 11.765 / 22.967 |
| Old PCM tail p50/p95, 6/6 | 0.681 / 0.760 | 7.875 / 9.180 | 0.740 / 4.917 |
| Requested handle interruption p50/p95 | 3.123 / 3.818 (6/6) | 4.310 / 7.802 (3/6) | 3.535 / 4.878 (6/6) |
| Observed logical cache peak, decimal GB | 0.75 | 0.38 | 0.75 |
| Logged completion requests | 11 | 10 | 11 |
| Sum of reported remaining prompt tokens | 3,670 | 3,846 | 3,670 |
| First-to-last prefill progress log p50/p95 | 1.468 / 8.523 | 1.605 / 8.574 | 1.450 / 8.517 |

Fulfilled-final-reply time has the frozen effect/final-fact/finish gate described above. It is not an all-checks case pass: three one-entry interruption failures and all four trace failures remain separately visible. The final two-entry arm also has a 4.917-second old PCM tail in its first cancellation despite a requested handle interruption. A request-state pass is therefore insufficient evidence of a fast audible stop. Mutation-success-to-result-text p50/p95 was 2.066/4.446, 1.864/79.613 and 1.630/4.135 **ms**; corresponding PCM was 1.248/1.610, 1.255/1.810 and 1.329/1.712 seconds.

| Final input to fulfilled reply finish, seconds | Two entries, first | One entry | Two entries, return |
| --- | ---: | ---: | ---: |
| Cancellation 1 | 9.681 | 21.385 | 11.765 |
| Airport return 1 | 22.281 | 20.852 | 22.967 |
| Airport return 2 | 20.423 | 25.166 | 20.705 |
| Cancellation 2 | 9.689 | 24.253 | 10.112 |

The candidate's completion p50/p95 exceed the first baseline by 11.696/2.885 seconds and the return baseline by 9.619/2.198 seconds. Baseline-to-baseline drift is 2.076/0.687 seconds. One candidate return case is faster than both corresponding baselines; no case is discarded to simplify the conclusion. The screen fails its quality gate and its aggregate no-slower gate. Production remains unchanged.

Cache logs show a maximum of one retained entry in the candidate and two in both baselines. They measure logical retained cache state, not allocated or resident memory. Reported remaining prompt tokens are 176 higher in the candidate despite one fewer server request; repair work and different adaptive requests prevent treating this as an isolated per-request causal estimate. Progress-log intervals exclude unlogged work. Full adaptive prompt equality, complete prefill service time, generated-token counts and GPU-only timing are not established.

## Recognition and memory boundaries

All thirty recognitions and three separate warmups return. The audits retain sixty-six outer requests, 330 stages and sixty-six PCM files, with no dropped observations, pending native calls or consistency errors. The same source WAVs produce **zero cross-arm exact submitted-PCM matches for the same declared case/turn**; all thirty recognition calls remain in the full denominator. Endpointed PCM identity must not be assumed from input file identity.

| Recognition/resource observation | Two entries, first | One entry | Two entries, return |
| --- | ---: | ---: | ---: |
| API-to-caller p50/p95, seconds (10 each) | 1.222 / 3.070 | 6.592 / 19.719 | 1.593 / 5.100 |
| Generate host-call p50/p95, seconds | 1.122 / 2.979 | 6.487 / 19.614 | 1.512 / 5.019 |
| Maximum submitted-to-worker queue, ms | 0.301 | 0.500 | 1.420 |
| Memory samples | 338 | 501 | 361 |
| System available start/end, decimal GB | 5.312 / 2.201 | 7.920 / 3.434 | 7.328 / 5.123 |
| System available minimum, decimal GB | 0.274 | 1.043 | 0.554 |
| System swap start/end, decimal GB | 5.592 / 10.127 | 9.238 / 11.586 | 7.392 / 10.360 |
| System swap minimum/maximum, decimal GB | 5.592 / 10.604 | 9.229 / 12.526 | 7.375 / 11.374 |
| Maximum observed owned RSS sum, decimal GB | 5.596 | 6.068 | 5.801 |
| Maximum LLM-process RSS, decimal GB | 4.852 | 4.859 | 4.896 |

The slowest one-entry recognition is the office correction in the second return case: 19.719 seconds API-to-caller, including 19.614 seconds in the generate host-call boundary and 0.079 ms between submission and worker entry. Available memory during that call is 1.654–1.855 GB across 38 samples; the suite minimum occurs elsewhere. The slowest corresponding observations in the first and final two-entry arms are initial airport requests, with 6 and 10 memory samples and available-memory ranges of 1.457–1.694 and 1.438–1.723 GB. These are different roles/PCM, not matched recognition inputs.

The observer records 387 measured hooks per arm: totals 5.196, 3.340 and 3.412 ms, maxima 1.027, 0.347 and 0.365 ms. This is partial measured overhead, not all instrumentation cost. Nested transcribe/generate timings must not be summed. The host generate boundary can include lazy execution, synchronization and memory movement; it is not GPU-exclusive.

RSS observations exclude the separate recorder clients, may omit a process between lifecycle samples, and can double-count shared pages. Two terminal samples retain worker-no-longer-exists errors. Fresh processes do not reset system swap, file caches, background applications or thermal state. Neither lower logical cache retention, higher available memory nor these slow calls establishes paging causation, actual memory savings or a live-room capacity limit.

## Verification and next work

The [post-inference receipt](../results/iteration17/post-inference-validation.json) freshly hashes the same ten model reference files once after all inference, checks 150 originals and 150 snapshots before and after those reads, verifies 41 frozen inputs per arm, and observes all 24 recorded PIDs absent in both phases. These are observed local reference bytes, not publisher authentication. The [verifier review](../results/iteration17/verifier-review.json) identifies a method edge case: interruption could bypass its exception handler and leave an incomplete success receipt. The executed run exited 0 with every required phase present; the separate final check requires those phase inventories explicitly. The executed method and receipt remain unchanged.

The recorded commands are `.venv/bin/python results/iteration17-cache2-before/run_suite.py`, then the corresponding `iteration17-cache1` and `iteration17-cache2-after` scripts, each after its own `preflight.py` and followed by `check_owned_exit.py`. Only after every timed stack exited did each arm's `run_received_audio_supervised.py` run, once and sequentially. Actual argv, environment settings, deadlines, source/model/input hashes and raw process outcomes are preserved in the arm plans and launch/run receipts. The scripts refuse to overwrite evidence; another replication requires fresh output directories and freshly declared/frozen path bindings.

This study precisely bounds the missing-identity issue and rejects a smaller-cache adoption. The next priorities are independent acoustic pause/disfluency/ambiguity cases, why detected followup speech can leave old replies running while recognition is pending, and a bounded simultaneous live-room study. Policy/acknowledgment ablations, adapted FDB access/evaluation, organizer requirements and human microphone/echo acceptance remain open. No current result justifies broader full-duplex or held-out accuracy claims.
