# Complete navigation summaries after redundant lookups

Iteration15 activated every requested route but omitted the replaced office from one final reply. A successful destination lookup was independent of the route computation in that plan, so the responder fell back to model synthesis. This iteration permits the existing confirmed-action summary when every otherwise-unused call is a successful lookup returning exactly the activated destination. It also checks the actual destination of a computed route before using that call's argument as a destination alias; a route used only to supply a waypoint cannot name the main destination.

The exception requires a nonempty literal query, one named place and a canonical ID equal to the activated destination. Other independent reads, ambiguous or malformed results, additional mutations, stale results and uncertain outcomes keep the existing handling. The complete original request still controls eligibility: requests for details, additional questions and unfamiliar wording retain model synthesis with all results. The existing replacement wording, ETA and thirty-word ceiling are unchanged. Only `agent/coordinator/responder.py` changes production behavior; the planner, executor, recorder, model settings and audio inputs match iteration15.

## Declared experiment and component evidence

The [plan](../results/iteration16/plan.json) and [protocol](../results/iteration16/protocol.json) retain the same cancel/return/return/cancel order, ten effects, six interruptions and four mandatory final speech outcomes. Every capture and physical attempt remains included. Whole received streams are transcribed once after the timed services exit, with no trimming or retries. Fulfilled-case timing requires both the whole case's backend effects and its final speech facts; a finished but incomplete reply remains a failure with a null fulfilled-case time.

Independent-place regressions reproduce six failures against the exact frozen iteration15 responder, with thirty-two other cases passing. An earlier pytest collection error is retained separately and is not behavioral evidence. The candidate passes **65 focused checks** and **655 tests in 5.42 seconds** overall. These include wrong destinations, ambiguous lookups, malformed results, requested extra details and a waypoint-source alias regression. The [independent review](../results/iteration16/independent-code-review.json) found no blocker within this narrow scope; it reviewed the source and existing receipts without claiming an independent test rerun. The [source comparison](../results/iteration16/change-isolation.json) records the single production-file change.

Four serial cases using reused synthetic development audio cannot establish held-out accuracy, general language fidelity, concurrent-room capacity or a causal latency improvement. Client PCM receipt and speech-handle state also do not establish physical audible stop time.

## Results and retained failures

| Predeclared outcome | Iteration15 | Iteration16 |
| --- | --- | --- |
| Completed captures / declared | 4/4 | 4/4 |
| Exact finalized input transcripts | 10/10 | 10/10 |
| Requested navigation effects | 10/10 | 10/10 |
| Complete backend cases | 4/4 | 4/4 |
| Interrupted earlier result handles | 6/6 | 6/6 |
| Complete final speech facts | 3/4 | 4/4 |
| Complete return replies | 1/2 | 2/2 |
| Full trusted client/server trace equality | 4/4 | 2/4 |

The [effect audit](../results/iteration16/audit.json) retains twenty-three successful physical attempts and one provenance-checked static search reuse. All ten effects succeed, both returns activate distinct fresh routes through airport→office→airport, and both cancellations clear the active route. The [plan audit](../results/iteration16/semantic-repair-audit.json) records nine initially accepted plans and one successful repair. In the first return's third turn, a compute-only plan is rejected before dispatch; the accepted compute→start plan follows **4.095 seconds** later. All eleven parsed-plan receipts remain recorded; these are not counts of physical model requests or parser retries.

The [whole-stream review](../results/iteration16/received-audio-review/qualitative-review.json) verifies twenty raw/resampled file hashes and all four final replies. Both return replies explicitly confirm airport navigation replacing the office and an eighty-eight-minute estimate. Both cancellation replies confirm airport cancellation. The proper-name differences Kempegowda→Kempegauda and Manyata→Maniata remain verbatim. This is a local ASR proxy, not human listening; initial and middle replies were deliberately interrupted.

Two full-trace-equality checks fail and remain failed. The [independent packet review](../results/iteration16/missing-identity-review.json) identifies each missing trusted event as the initial listening packet in a return case. Both raw packets have no participant object and an empty native sender identity; the recorder rejects them. A participant-connected callback follows approximately 1.845 ms and 0.374 ms later. Trusted session-start events, readiness and input publication follow normally. Decoded raw content matches all four server traces, but that content cannot establish the sender identity of either rejected packet. No transport loss is inferred, and the rejected packets are not retroactively promoted from their payloads. The remaining identity-boundary limitation needs separate treatment.

The [supplementary audit](../results/iteration16/supplementary-log-review.json) records **263 server events**, 263 accepted/flushed trace writes, 261 promoted client events, two identity-missing rejections and zero buffered packets. It retains **42 warning rows**, one real LLM `BrokenPipeError` while writing the SSE terminator, and four server `JS_FAILED` job statuses. These remain distinct from successful application cleanup: all four agents depart, the worker exits zero without escalation, and all eight owned suite/reviewer PIDs are subsequently absent. One terminal memory-sampling row records that the worker has already exited.

## Timing and operating limits

[Client receipt timings](../results/iteration16/latency-summary.json) use scheduled RMS-threshold input speech end and nearest-rank percentiles. Initial acknowledgment PCM p50/p95 is **3.154/4.515 seconds**, substantive PCM **9.194/17.244 seconds**, and mutation success **8.009/16.135 seconds** (4/4). Follow-up acknowledgment PCM is **3.320/3.679 seconds**, substantive PCM **9.620/14.920 seconds**, and mutation success **8.128/13.212 seconds** (6/6). All ten mutation-success-to-result-text observations are present, at p50/p95 **1.229/4.954 ms**; result PCM follows at **1.185/1.708 seconds**. These observations do not establish a causal speedup over the prior fixed-order suite.

All four cases meet the backend-effects and final-speech conditions of the [fulfilled-case completion metric](../results/iteration16/completion-latency.json), with final-input end to reply finish p50/p95 **9.859/22.642 seconds**. The two return cases take **22.642 and 20.268 seconds**. This metric does not require full-trace equality, so the two attribution failures remain separately reported; it also does not measure the whole conversation from its initial request. The six old-result PCM tails span **0.680–0.778 seconds** after follow-up onset, while handle interruption p50/p95 is **3.170/3.909 seconds**. Neither measures physical audible stop.

The [independent STT audit](../results/iteration16/stt-stage-independent-audit.json) verifies eleven native calls, including warmup plus ten recognitions, all 110 stage records and 22 PCM files. All ten recognized texts match the finalized input events, with no dropped records, observation errors or pending calls. The slowest recognition is the second return's initial request: **2.562 seconds** API-to-caller, including **2.479 seconds** in the generation boundary, with a **0.377 ms** dispatch-to-worker delay. The observer is byte-identical to iteration15. Host boundaries can include lazy execution and synchronization; they are not exclusive GPU timings.

Across 341 samples, available system memory reaches **0.161 GB**, and swap grows from **5.214 to 9.614 GB**, peaking at **10.737 GB**. These are whole-system levels, not per-process paging measurements or proof of recognition-delay causation.

## Resource investigation

A separate [read-only investigation](../results/iteration16/resource-investigation.json) inspected the installed MLX-LM implementation and retained measurements without changing this experiment. Requests with seed seven use sequential serving, where the requested 256 MiB cache byte limit is not enforced. The two-entry limit is enforced; iteration15 logs report up to 0.75 GB of logical retained cache. This is not a measured total allocation or resident-memory limit.

The historical slow recognition calls spend most of their measured time inside the generation boundary, with sub-millisecond dispatch queues. Their submitted PCM differs across runs, and each suite's minimum available memory occurs outside its slowest recognition window. The observations therefore do not establish memory pressure as the cause. A future fresh-process two-entry/one-entry/two-entry screen is specified, including the expected extra-prefill tradeoff and all useful-work, memory and timing denominators. It has not been run or adopted here.

## Evidence and decision

The [snapshot](../results/iteration16/source-snapshot.json) preserves 95 source/config/test/method files. [Post-inference validation](../results/iteration16/post-inference-validation.json) freshly matches ten complete model files to the preflight reference hashes and all 39 run-frozen files before and after model reads, then confirms all eight owned PIDs absent. Matching observed bytes does not authenticate publishers. The [suite controller](../results/iteration16/run_suite.py), [preflight](../results/iteration16/preflight.py), protocols, model receipts and raw outputs retain the actual commands and environment; reruns require fresh output directories and machine-specific rebinding.

Retain the responder change: the six reproduced regressions, independent review and complete generated/received return replies support the narrow fix. Preserve the earlier speech failures and this iteration's two attribution failures. The next work is to bound the missing-identity behavior, then test the proposed cache-entry tradeoff and independent acoustic cases. The [goal audit](completion-audit.md) retains the broader benchmark, organizer, concurrency, policy-comparison and delivery requirements.
