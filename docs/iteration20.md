# Iteration20 — grounded route information and release checkpoint

## Change

A complete recognized distance or driving-time question now requires one matching route computation. A place lookup alone cannot provide the requested metric. The existing planner gets one semantic repair attempt; the validator never inserts calls. Supported information questions reject navigation mutations before dispatch, including when another route is already active.

The change is confined to `agent/tools/car_tools.py`. It recognizes whole, unqualified question forms and uniquely grounded existing place names, IDs or aliases. Unknown, ambiguous, qualified, compound, current-route and nondefault-unit questions retain ordinary planning. Recognition, speech synthesis, response generation, the two-entry planner cache, the earlier clarification continuation and recognition-aware playback remain unchanged.

## Component verification

The full Python suite passes **797 tests in 6.02 seconds**, with all 86 recorded source/configuration paths unchanged. The new route-information file contains 42 cases covering supported grammar, matching direct/reference plans, incorrect and mutating plans, unsupported forms, one repair, preservation of an active route and stop, context consumption, state-sensitive reuse and reentrant cancellation. The archived baseline fails 22 of these cases and passes 20 before the production change. The baseline reproduction is development evidence, not held-out evaluation.

An initial syntax error and an incorrect test expectation remain in the saved failed outputs. The corrected test preserves the full merged request while requiring cancellation-only results. The final targeted suite passes 130 checks. Independent scoped reviews found no concrete production blocker.

Fresh frontend verification passes six event/map/trace tests and the production build with source files unchanged. These checks do not establish live microphone or speaker behavior.

## Acoustic protocol and deviation

The original protocol declares six conversations per arm: hospital distance after ambiguity, airport distance, office driving time, hospital distance with airport navigation active, unresolved Airport Road ambiguity, and explicit hospital navigation. Seven first-render inputs are shared. All 157 original source files, their snapshots and ten model references were frozen before rendering.

The baseline completes its first paired conversation. Its second client exits during argument parsing because the launcher passes a follow-up-only option to a single-question test. No room, audio or capture report exists for that client; the remaining four baseline cases never launch. The suite stops, and its worker, model server and transport close. This is a harness failure, not an agent response failure.

The original frozen launcher and partial baseline are preserved. A separate v2 launcher passes the follow-up option only for cases containing a follow-up and additionally checks its own amended method hashes. Candidate cases use their first capture attempts. Only the first conversation has a matched baseline; all five missing baseline captures remain in the twelve-case denominator. No complete six-pair comparison is claimed.

The [amendment](../results/iteration20/harness-amendment.json), [original protocol](../results/iteration20/protocol.json), [test receipt](../results/iteration20/tested-source-v1.json), and [frontend receipt](../results/iteration20/frontend-release-validation.json) retain the method and verification details. Final acoustic results are recorded below after inference and speech review finish.

## Observed outcomes

| Case | Baseline | Candidate |
| --- | --- | --- |
| Ambiguity → hospital distance | Correct clarification, then lookup-only reply saying distance is unavailable | Correct clarification, matching route read and received 2.2km/five-minute answer |
| Airport distance | Client parser failure; no room or input | Matching route read and 35.4km/88-minute answer |
| Office driving time | Unlaunched | Correct recognition, but both plans use unsupported `the office`; both rejected, zero dispatch, asks for retry |
| Active airport → hospital distance | Unlaunched | Fresh airport start at 88 minutes, then hospital read/2.2km answer with no navigation mutation after query |
| Ambiguous Airport Road distance | Unlaunched | Times out waiting for trusted session readiness; source input is never published, received transcription is empty |
| Explicit hospital navigation | Unlaunched | Fresh compute/start, active hospital route and five-minute received estimate |

Four of six candidate final tasks are fulfilled. Five candidate captures complete, but the office reply fails the task. The sixth capture fails before input publication. Across both arms, there are seven room capture reports, six completed captures, one parser-failed client and four unlaunched cases. Nine of sixteen declared input publications occur. All twelve physical attempts remain: ten done and two ambiguous-lookup errors. Candidate information reads pass three of four declarations; both candidate navigation effects succeed. No full baseline comparison or population success rate is claimed.

The [speech review](../results/iteration20/speech-fact-review.json) compares complete received-stream ASR against declared facts and actual results. The airport transcript uses `35, 4 kilometers` and `Kempegauda`; the backend and generated reply use 35.4km. Those literal ASR variants are preserved, and there is no human-listening claim. All seven captured streams are transcribed, including the silent failed-startup room; empty transcription completion does not count as interaction success.

The [acoustic audit](../results/iteration20/acoustic-audit.json) retains all twelve declared rows and five missing baseline capture reports. Raw decoded/server equality is 7/7; trusted equality is only 2/7. The failed ambiguity room receives initial agent/session readiness packets with empty native identity, rejects them, and never publishes input. Later trusted user-state delivery does not recover the missing readiness event. Existing fields provide no safe basis to authenticate those earlier packets; the upstream cause remains unproven.

Both workers and transport services exit normally; both model servers exit after termination. All seven observed rooms depart and flush their traces. The [release post-check](../results/iteration20/post-inference-validation-release.json) verifies all ten model references, 157 original source files and snapshots, seven shared inputs, selected package/resource bindings, and nineteen absent phase/service/client PIDs. This verifies the partial experiment's bytes and cleanup; `full_declared_protocol_completed` remains false. The earlier full-protocol and intermediate partial verifiers remain unexecuted source artifacts; their presence is not a validation result.

## Latency and release limits

The [timing review](../results/iteration20/latency-review.json) retains all twelve declared final-input anchors. Source activity-end to received substantive PCM is measured for one baseline reply and five candidate replies. Baseline observed reply completion is 24.694s, but it is an incomplete answer, so its fulfilled completion stays null. Candidate observed completion p50/p95 is 26.583/31.292s across five finished replies; fulfilled completion has the same values across four successful replies. The four individual fulfilled values are 22.151, 26.583, 29.148 and 31.292s. Three successful current information-read dispatch intentions occur at 7.489, 13.340 and 12.997s. Acknowledgment PCM remains unmeasured. These are client/source timing observations, not microphone endpoints, physical speaker timing or a demonstrated speedup.

Retain the route-information guard: it supplies three grounded information answers and prevents navigation mutations during those queries. The office place-form failure and unauthenticated-startup failure remain release limitations, alongside earlier proper-name recognition problems. This is a tested development checkpoint, not a claim of general stability, production readiness or completion of the broader research objective. Further feature work is deferred while preparing the requested GitHub commit/PR.

For reproduction, use the frozen protocol, model/runtime receipts and source snapshots. The saved commands are in each `*-supervision*.json`, `run-report.json` and launch receipt. Output directories are intentionally exclusive; repeat experiments must use new directories and preserve prior attempts. The original baseline launcher is known to reject single-input cases; the separately saved v2 candidate launcher records the corrected argument construction and amendment. Running the original full six-pair protocol has not been completed.
