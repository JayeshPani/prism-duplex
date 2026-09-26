# Rejecting incomplete navigation plans before dispatch

Iteration14 preserved a request that correctly recognized a return to the airport but only computed a route. The office route remained active. This iteration tests a narrow plan contract and one repair attempt before any rejected plan reaches tool execution. It keeps the responder, recorder, model settings and source audio unchanged so the separately observed missing replacement phrase remains visible in the scoring.

The car contract covers supported whole navigation commands whose destination resolves uniquely on the existing map. It checks the requested destination, permits exactly one navigation start without cancellation or waypoint mutations, and requires activation to reference a route computed by the current plan. Incomplete and no-call plans, ambiguous or unfamiliar destinations, questions, negation and compound wording retain existing planner handling. This is a bounded check, not proof of general natural-language plan completeness.

On a domain rejection, the coordinator records the rejected parsed JSON and reason, then permits one repair using the entire original request and normal conversation/slot/outcome context. It never appends a write to a rejected plan. Failed repair follows the existing no-dispatch failure path. Current-intent checks and gate cancellation cover corrections while repair is pending, including corrections raised synchronously by event subscribers. Parsed rejection events do not reveal every raw network response or parser-level JSON repair attempt.

## Declared experiment

The [protocol](../results/iteration15/protocol.json) reuses all iteration14 WAV bytes in the same cancel/return/return/cancel order. All ten effects and all final speech facts remain mandatory, including replacement of the office in a return reply. All six pairwise interruptions and all captures, rejected plans, repair failures and physical attempts remain in the evidence. Four serial synthetic development cases cannot establish population accuracy or a causal latency improvement.

The [completion derivation](../results/iteration15/summarize_completion.py) separately measures final-input speech end to reply finish only when the whole case meets its backend effects and final speech facts. Unfulfilled but finished replies remain visible and have null fulfilled-case times. A fixture using isolated copies of iteration14 preserves four finished final replies, two fulfilled cancellation cases and two failed return cases. The first fixture run exposed a `/var` versus `/private/var` path-resolution bug in this new helper; the corrected method and both receipts are retained. No historical artifact or model run was changed.

## Results

The complete suite passes **617 tests in 5.40 seconds**, including **107 focused contract/coordinator/resolver checks**. Independent review found no blocker for this narrow scope; its sixty pure-validator probes and separate manual async review are described in the [review receipt](../results/iteration15/independent-code-review.json). The [change comparison](../results/iteration15/change-isolation.json) confirms only five production files changed; responder, recorder and configuration bytes match iteration14.

| Predeclared outcome | Iteration14 | Iteration15 |
| --- | --- | --- |
| Completed captures / declared | 4/4 | 4/4 |
| Exact finalized input transcripts | 10/10 | 10/10 |
| Requested navigation effects | 9/10 | 10/10 |
| Complete backend cases | 3/4 | 4/4 |
| Interrupted earlier result handles | 6/6 | 6/6 |
| Complete final speech facts | 2/4 | 3/4 |
| Complete return replies | 0/2 | 1/2 |

The [effect audit](../results/iteration15/audit.json) retains **23 successful physical attempts**, one provenance-checked static search reuse, all ten effects and no failed core checks. Both return cases activate distinct fresh routes through airport→office→airport; both cancellations clear the active route. The static lookup reuse remains separate from real dispatches and fresh route computations.

The first return reproduces the actual compute-only model plan. The [semantic repair audit](../results/iteration15/semantic-repair-audit.json) retains the rejected parsed JSON at server line 64, the repaired compute→start plan at line 65, and subsequent dispatches at lines 72 and 74. No tool starts between rejection and acceptance; R3 activates the airport and replaces the office. The measured rejection-to-acceptance interval is **4.293 seconds**. Nine initial plans and one repair are accepted, giving eleven parsed-plan receipts, zero fallbacks and no audit invariant failures. Parsed receipts are not physical HTTP/model request counts; parser retries are unmeasured here.

The [whole-stream review](../results/iteration15/received-audio-review/qualitative-review.json) checks all four untrimmed received streams and twenty raw/resampled hashes. The repaired return confirms airport navigation, replacement of the office and an eighty-eight-minute estimate. The other return again omits the replacement fact in both generated and recognized speech, despite correct backend state. That case remains a spoken-content failure. Proper-name differences remain verbatim, including Kempegowda→Kempegauda and Manyata→Maniata. This is an ASR proxy, not human listening.

## Timing and operating limits

[Client receipt timings](../results/iteration15/latency-summary.json) use scheduled RMS-threshold input speech-end and nearest-rank percentiles. Initial acknowledgment PCM p50/p95 is **3.400/10.146 seconds** and substantive PCM **9.399/17.560 seconds** (4/4). Follow-up acknowledgment PCM is **3.319/3.820 seconds**, substantive PCM **9.700/16.339 seconds**, and mutation success **8.118/13.799 seconds** (6/6). Received response timing includes the incomplete reply; it cannot alone establish task success. All six earlier PCM tails remain observed, spanning **0.719–0.779 seconds** after follow-up onset; speech-handle interruption p50/p95 is **3.195/4.086 seconds**. Neither boundary establishes physical audible stop time.

The [fulfilled-case completion metric](../results/iteration15/completion-latency.json) retains the failed return as null. Three of four cases satisfy every backend effect and final speech fact, with final-input end to reply finish p50/p95 **9.899/23.489 seconds** among those three. The one complete return takes **23.489 seconds**; the other remains a failure despite its finished reply. This conditional statistic does not exclude the failed case from its declared denominator or measure the whole case from its initial request. The small fixed-order replay and model/runtime variation do not establish a causal latency improvement.

The [supplementary review](../results/iteration15/supplementary-log-review.json) verifies all **263 raw/promoted/server events**, with 263 accepted/flushed and zero rejected. No packet required deferred attribution, so live recovery from that race remains unexercised. It retains **35 warning rows**, one real LLM `BrokenPipeError` traceback while writing an SSE terminator, and four server `JS_FAILED` statuses. The observed task effects do not justify calling the logs error-free. All application cleanups finish, all agents depart, and the worker exits zero without escalation. Two recorder disconnect callbacks are absent; corresponding relative cleanup times remain null. All eight owned suite/reviewer processes are subsequently absent.

The [independent STT audit](../results/iteration15/stt-stage-independent-audit.json) verifies eleven native calls (warmup plus ten recognitions), all 110 stages and 22 PCM files. Submitted samples match the actual WAV-write bytes; all ten recognized texts match their finalized input events. Calls and outer requests return, with no dropped records, observation errors or pending native calls. The slowest recognition, the second return case’s initial request, takes **7.887 seconds** API-to-caller, including **7.785 seconds** in the instrumented generation boundary; recognition queue delay is at most **0.470 ms**. These host boundaries include lazy execution and are not exclusive GPU timings. The observer is byte-identical to iteration14; measured hook overhead excludes the rest of the wrapper and does not establish a causal diagnosis.

Memory pressure remains substantial: sampled available memory reaches **0.120 GB**, with peak swap **10.949 GB** across 365 samples. A slow initial transcript also takes **8.527 seconds** from scheduled input speech end. These concurrent observations do not establish that memory pressure caused recognition delay. Broader concurrent-room experiments need a bounded configuration and must retain shared-queue/resource failures.

## Evidence and decision

The [source snapshot](../results/iteration15/source-snapshot.json) preserves **94 source/config/test/method files**. [Post-inference validation](../results/iteration15/post-inference-validation.json) freshly matches ten complete model files to their reference hashes and all 39 run-frozen files before and after that read. Hash agreement freezes observed bytes; it does not authenticate publishers. Copied scripts require fresh output paths and machine-specific rebinding. The frozen core and supplementary audit helpers inherited an `iteration14` scope-label typo; their actual input paths, protocol and evidence hashes are under iteration15. The original output labels are retained and explained here, rather than changing frozen methods after launch.

The first final-validation receipt caught its own stdout file while that file was still being written, leaving one output-log hash inconsistent after exit. The original method, receipt and log are preserved. The revised finalizer captures stdout after execution and verifies completed artifacts; this correction changes no live capture, test result, production source or failure score.

Retain the narrow contract and bounded repair: independent regressions and one real rejected-plan intervention support the missing-activation fix. The full final speech facts remain the next correctness issue. No general planner completeness, physical microphone/echo behavior, simultaneous-room capacity, held-out benchmark accuracy or latency improvement is claimed. The [goal audit](completion-audit.md) keeps those requirements and the unresolved external-access/delivery steps open.
