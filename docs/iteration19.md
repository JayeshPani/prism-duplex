# Iteration19 — preserve navigation after a destination clarification

The grounded clarification change is retained. The matched hospital replay changes from a lookup-only reply to actual navigation with the five-minute ETA in received-audio transcription. Complete final outcomes are **1/5 baseline and 2/5 candidate**. Four place-recognition failures and two missing distance answers remain across the ten conversations; this is narrow development evidence, not general navigation success.

## Failure and hypothesis

Iteration18's hospital clarification finds the correct place but never computes or activates its route. The original navigation request remains in conversation history, but the latest fragment bypasses the whole-command plan validator. The [saved diagnosis](../results/iteration19/clarification-failure-review.json) binds that conclusion to two physical searches, zero navigation effects and the received partial reply.

The hypothesis is that an explicit, grounded continuation can preserve the requested action without inventing a tool chain. The normal planner should receive both the original request and the verbatim answer, and the same navigation contract should reject a lookup-only response before dispatch.

## Change and boundaries

The coordinator captures the most recent current execution for the next finalized turn. The car manifest can recognize a whole positive navigation request followed by a matching structured destination ambiguity and a full named answer. It supplies a canonical continued request alongside the original request and actual answer. The resolver validates the complete compute→start obligation and permits its existing single repair attempt. Empty or incomplete plans for a grounded complete answer are rejected too. All rejected plans remain in the trace; the helper never appends tool calls.

Context is consumed even when a cancellation, query or independent new command declines continuation. Merged uncommitted speech keeps its captured context; a stale execution or summary cannot restore it, and closure clears it. Summary generation retains the original request and answer, while the deterministic action summary uses the separately grounded continuation.

The original phrase must be an actual mapped ambiguity. Unknown, qualified, compound and model-only clarification requests remain ordinary planner behavior. An answer must be a full stored name, ID or existing alias; spoken `in`/`at` connectives remain supported. Unique name prefixes do not establish a complete answer. A named replacement outside the original candidate list is allowed. Successful, blocked and uncertain navigation writes forbid continuation; returned-error writes remain eligible under the current simulated backend's pre-effect error semantics. This is not a general pending-task framework or an exactly-once guarantee.

## Completed verification

- **755 tests passed in 5.78 seconds**, with all 85 recorded source/configuration paths unchanged during the run. [Test receipt](../results/iteration19/tested-source-v2.json).
- Five scripted full-conversation variants fail on the isolated original package because no navigation activation occurs. They pass on the candidate, alongside cancellation/query, compound-request, ambiguous/partial-name, prior-effect and lifecycle checks. The [baseline reproduction](../results/iteration19/baseline-regression.json) ran after candidate development against archived source; it is not held-out evaluation.
- Nine lifecycle checks cover consumed context, merged speech, resistant late execution, delayed summaries, shutdown, mutation of copied trace payloads and reentrant cancellation. Enforcing complete grounded answers initially exposed an overly broad prefix match; the [failed run](../results/iteration19/lifecycle-incomplete-regression.txt) is preserved and the name matcher was tightened.
- A test-fixture error from copying an execution's asynchronous tasks was corrected by copying only its data. The [failed test output](../results/iteration19/test-fixture-copy-failure.txt) remains archived.
- An opt-in recorder completion mode accepts a fresh, uniquely matched non-ack reply with received PCM, successful handle completion and the configured quiet tail. This allows a valid cancellation reply to finish capture. Default result-only completion remains; capture completion is not task accuracy. Its 73 focused checks include 13 added cases.

The [production review](../results/iteration19/production-review.json) records the exact reviewed files and limits. No source, failure or raw capture under iteration18 was rewritten.

## Live comparison

The [protocol](../results/iteration19/protocol.json) and [validation plan](../results/iteration19/validation-plan.md) declare five cases per arm: the exposed Airport Road→hospital failure, pending-request cancellation, Hebbal→Third Wave Coffee, a distance query after ambiguity, and Indiranagar→Ather Grid charger. There are ten captures, twenty planned input publications, six intended navigation effects and four cases requiring no navigation effect.

Both arms retain the iteration18 speech-resume guard and identical local model settings. The control imports a separate full package with the five pre-continuation modules restored; the candidate imports the current working package. Eight first-render speech assets are shared between arms. All failures, missing inputs and physical attempts remain in the denominators, and there are no replacement captures. Whole received-stream ASR follows both timed stacks, with semantic speech review separate from capture status.

This is a fixed-order development comparison. The hospital wording is deliberately reused; the map and prior failure informed the intervention. It does not establish held-out generalization, physical speaker timing, microphone/echo performance or concurrent-room capacity.

| Conversation | Baseline | Candidate |
| --- | --- | --- |
| Airport Road → hospital | Correct question, then lookup only; no activation or ETA | One grounded continuation, fresh compute and start for `P_HOSP`, active/version1, 2.2km and five-minute ETA; complete received final facts |
| Pending navigation → stop | Successful no-op cancellation; correctly says no navigation is active | Same correct no-op and final fact; no continuation |
| Hebbal → Third Wave Coffee | Recognizes `Hebel` then `Third wave coffee Hebble`; unknown-place results and no route | Same recognition strings and failure; continuation unexercised |
| Airport Road → distance question | Only lookup; truthfully says distance is absent instead of answering 2.2km | Same failure; no continuation or navigation mutation |
| Indiranagar → Ather Grid | Recognizes `Endure Inagar` then `other grid charger in Dureniga`; unknown places and no route | Recognizes `Endurinegar` then `other grid charger in Durenegger`; unknown places and no route |

All **10/10 captures**, **20/20 input publications** and **10/10 whole-stream ASR reviews** finish. Intended navigation effects are **0/3→1/3**; complete backend cases and final spoken facts are each **1/5→2/5**. Both cancellation cases retain their actual no-op calls without counting them as effects. Both query cases fail the explicitly required route-read check. Every initial request produces zero navigation attempts. Six initial replies ask the correct named-place question; four unknown-place replies reflect recognition failures. All 22 physical attempts remain: 8 done and 14 errors, with dependent undispatched errors recorded separately.

There are no rejected-plan events or semantic repairs in this live set. The candidate hospital case directly generates the complete search→compute→start plan. The two other positive candidate cases never establish the required original ambiguity and do not exercise the new continuation hook. Five positive scripted regressions broaden component coverage but do not make those acoustic failures pass.

The [acoustic audit](../results/iteration19/acoustic-audit.json), [received-speech review](../results/iteration19/speech-fact-review.json), and separate [baseline](../results/iteration19-baseline/baseline-mechanism-review.json) / [candidate](../results/iteration19-candidate/candidate-mechanism-review.json) reviews preserve each case. Speech judgments compare complete received-stream ASR with actual tool results; no human-listening or physical-audibility claim is made. Literal proper-name transcription variants remain. The candidate cancellation's queued acknowledgment is absent from whole-stream ASR and is not counted as heard.

## Provenance, resources and setup deviation

The source freeze preceded rendering. [Post-inference verification](../results/iteration19/post-inference-validation.json) matches all ten model references, 154 frozen source files, their 154 archived copies and eight audio assets after both timed stacks and ASR processes stop. All 21 recorded phase/service/client PIDs are absent. Both imported packages match their selected frozen files, including the common speech guard and shared Kokoro model directory. Recorder-versus-worker differences in the baseline are explicit provenance differences, not hidden source equality.

The candidate's first setup exits before launching any service, publishing audio or starting a capture: a socket bind returns `Address already in use`. No listener is observed afterwards, and all four ports bind successfully before the retry. Transient socket reuse is plausible, but the failing port/kernel state was not captured, so the cause is unproven. The [recovery receipt](../results/iteration19/candidate-setup-recovery.json) archives the four original files byte-for-byte under `iteration19-candidate/startup-attempt1`. **One post-freeze setup retry is a disclosed deviation from the no-retry plan**. The runtime, models, inputs, case order and ten first capture attempts are unchanged; no failed conversation is replaced. Its extra exited PID is checked separately from the 21 normal phase/service/client PIDs.

Both arms close all five rooms and flush accepted trace events, then exit worker/LiveKit normally and terminate the LLM server. All ten raw decoded traces match the server. Trusted equality is **2/5 baseline and 4/5 candidate**; missing native identity is still rejected. Capture completion, raw equality and application cleanup do not erase these four trusted-trace failures or server `JS_FAILED` labels.

System available-memory minima are **0.360GB baseline / 0.863GB candidate**; system swap peaks are **10.146GB / 11.812GB** (decimal units). These are whole-host observations, not attributable allocation or room-capacity estimates. Both arms observe ten native recognition calls plus one warmup without dropped observations. The candidate hospital answer alone takes about 11.74s in native recognition. Warnings, a baseline no-sample watchdog event, transport closed-pipe error and cleanup-time process-sampling races remain in the mechanism reviews. Fixed order and differing host pressure prevent a causal performance claim. The LLM's two-entry cache is unchanged; its requested byte cap remains unenforced on the seeded sequential path.

## Latency observations

The [timing analysis](../results/iteration19/latency-review.json) retains all ten final-answer anchors, including failed tasks. Times start at each final input's last above-threshold source sample and end at client observations; they are not microphone endpoints or physical speaker measurements. Nearest-rank p95 is the maximum in these tiny groups.

| Seconds after final input speech end | Baseline p50 / p95 (measured) | Candidate p50 / p95 (measured) |
| --- | --- | --- |
| Acknowledgment queued | 2.441 / 2.530 (5/5) | 3.498 / 17.471 (5/5) |
| Acknowledgment received PCM | Unmeasured (0/5) | Unmeasured (0/5) |
| First substantive received PCM, including failed answers | 5.880 / 7.902 (5/5) | 8.340 / 21.538 (5/5) |
| Observed final reply finished, including failed answers | 10.789 / 12.473 (5/5) | 13.200 / 27.233 (5/5) |
| Fulfilled final reply finished | 6.937 / 6.937 (1/5) | 22.113 / 27.233 (2/5) |

The only baseline navigation-call dispatch is the no-op cancellation, at 3.987s. Candidate cancellation dispatch is 18.576s; the successful hospital start dispatch is 20.375s, with its full reply finished at 27.233s. These are physical-call logger dispatch intentions, not actual backend entry/effect times. The other seven fulfilled-completion values remain null. There is no latency improvement claim: the candidate observed replies are slower in this fixed-order run, and different successful-task mixes make fulfilled percentiles unsuitable as a speed comparison. This suite does not include playback interruptions; iteration18 remains the relevant narrow interruption evidence.

## Remaining work

Keep the grounded continuation and its narrow boundaries. Next correctness work should address the missing distance read and investigate place-name recognition without silently adding aliases for these exposed clips. The broader goal still includes simultaneous rooms, policy/acknowledgment comparisons, organizer requirements, adapted FDB evaluation and actual PR publication after user-selected staging. All earlier failures remain archived; this checkpoint does not complete the broader objective.
