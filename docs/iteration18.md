# Recognition-aware speech pauses and new acoustic development cases

**Decision: retain the recognition-aware output guard, with the failed cases preserved.** The delayed-stop control resumed the old reply and let it finish; the candidate kept it paused until recognition returned, then interrupted it. The independent language set also exposed a missing navigation action after clarification. This checkpoint does not establish general conversational reliability.

## Mechanism and change

Iteration17 retained three old replies that finished during late recognition, plus two long received-audio tails despite interruption requests. The [historical reconstruction](../results/iteration18/interruption-evidence-review.json) reviews all eighteen interruption pairs. Five slow targets return to SDK “speaking” about two seconds after user-listening while native recognition is pending. It preserves both broad heuristic old-audio windows and explicitly tagged frames. Historical event timing supports the hypothesis; it does not itself prove the SDK classified those pauses as false interruptions.

The installed livekit-agents 1.8.3 implementation (RTC 1.1.18) starts its false-interruption timer when VAD ends. With no transcript yet, non-streaming recognition does not supply the end-of-turn task that would defer that timer. The [source reproduction](../results/iteration18/false-resume-reproduction.json) exercises the actual timer/recognition methods and confirms resumed old speech before a delayed final. A nonempty final subsequently interrupts; an empty final does not. This reproduction uses a controlled sink and recognition state, not microphone input or model inference.

An event-only re-pause was rejected: once the SDK resumes its audio sender, an already awakened waiter can leak a frame even if the callback immediately pauses again. The [corrected source audit](../results/iteration18/sdk-interruption-boundary-audit-v2.json) explicitly retracts the earlier feasibility claim without rewriting that original report.

The candidate wraps the public audio output before its resume reaches the sender. It tracks the captured speech handle and room-local awaited recognitions. A completed empty recognition permits the original reply to resume; a nonempty final, failed/cancelled recognition or thirty-second recognition-wait deadline interrupts the captured reply and waits for its actual generation cleanup before releasing audio. It preserves ordinary SDK resume decisions outside an owned hold, protects later handles from late callbacks, and closes its callbacks/timer with the session. The thirty-second deadline is an engineering choice, not an optimized conversational threshold or a cap on total pause duration. Warmup and whole-span retranscription do not count as awaited room recognitions; cancelled native inference can continue after its awaiting task ends.

No streaming transcript or SDK patch is introduced. Pending recognition is counted per room, not correlated to a particular utterance. A newly SDK-paused handle can therefore acquire a hold while an older recognition in the same room remains pending. The SDK's agent state and `agent_false_interruption.resumed` event report its own decision; `recognition_pause` records when the output guard actually defers that decision. Neither is physical speaker timing.

## Validation before live inference

The final production/recorder revision passes **696 Python tests in 5.87 seconds**, with all 79 recorded source paths unchanged during the run. Earlier runs and the one failing ordinary-resume test remain preserved; the failing condition was fixed before the final run. The experiment-only delay adapter has eleven separate model-free checks.

The [final SDK generation component](../results/iteration18/generation-pause-final/generation-pause-component.json) passes five cases using actual `AgentSession` generation and the installed room-output forwarder. Its event-only control reproduces a leaked frame. Nonempty/error/timeout cases retain only the pre-pause frame; empty recognition finishes all original frames; replacement speech completes after cleanup. A fake native audio source has zero queue, so these checks do not establish native buffering, RTC cutoff or acoustic intelligibility.

## Declared comparison

The [protocol](../results/iteration18/protocol.json) selects 21 captures: eight matched probes with a transparent output control, then thirteen candidate captures. The candidate's five extra cases cover a fully specified hospital, 250 ms and 1200 ms inserted pauses before a metro destination, a disfluent self-repair, and Airport Road ambiguity followed by a hospital clarification. The eight probes per policy comprise explicit stop speech, silence, band-limited noise and artificial breath-shaped noise, each under native recognition and a five-second asynchronous delivery hold.

There are 38 intended input publications, 25 expected navigation mutations, 21 final replies, four intentional stop interruptions and twelve non-speech controls. Every missing capture, extra call, failed effect, hallucinated transcript and incomplete reply stays in the denominator. The artificial breath stimulus is shaped noise, not recorded human breathing. No VAD response means the control did not exercise false-interruption recovery.

Both policies use the current recognition counters, recorder, diagnostics, model bytes and two-entry LLM cache. The control forwards SDK pause/resume normally; the candidate adds recognition gating. The artificial wait occurs after the observed engine call but before LocalSTT ends its pending recognition. It applies to every successful recognition in the selected room, including initial and empty results. Warmup and whole-span repair remain excluded. This measures delayed delivery, not naturally slow native inference or cache pressure.

Input speech uses the first untrimmed local Kokoro render of each declared stem. Pause cases reuse the exact stems; inserted silence and the larger measured acoustic gap remain distinct. No preparation ASR/VAD selection is performed. The new wording is independently composed development data informed by prior failures and known map semantics, not untouched held-out evaluation.

The recorder's opt-in completed-response trigger enables clarification recovery while preserving the existing result-audio default. Each case has a fresh room and fixed deadlines. The suite must verify departure before selecting the next condition. Source/input changes fail the run, including changes during final cleanup. Whole-stream received-speech ASR runs once per capture only after both timed stacks exit; semantic review remains separate from capture completion and automatic effect checks.

Fixed arm order, different preceding language cases and shared host resources limit performance comparisons. The 1000 ms cutoff screen uses received PCM and a 300 ms quiet interval; it is an engineering screen rather than physical audible timing. Human microphone/echo, simultaneous rooms, official FDB evaluation and organizer Theme 05 compliance remain separate open requirements.


## Retained live outcomes

The [mechanical audit](../results/iteration18/acoustic-audit.json) preserves all 21 declarations and 63 physical attempts: 62 done and one expected ambiguous-place read error. It extracts every case without an evidence/parsing gap. The [independent speech review](../results/iteration18/speech-fact-review.json) checks content separately from capture completion, interruption flags and backend state.

| Outcome | Control | Candidate |
| --- | ---: | ---: |
| Scheduled captures | 8 | 13 |
| Completed captures | 7/8 | 13/13 |
| Inputs actually published | 14/16 | 22/22 |
| Expected navigation effects | 9/10 | 14/15 |
| Cases with required effects | 7/8 | 12/13 |
| Complete final spoken facts, ASR proxy | 7/8 | 12/13 |
| Required old replies interrupted | 1/2 | 2/2 |
| Received cutoff screen for voiced stops | 1/2 | 2/2 |
| Full trusted trace equality | 2/8 | 8/13 |
| Full raw decoded/server equality | 8/8 | 13/13 |

The control's native-noise capture times out before authenticated session readiness, so neither input is published. Its first listening and session-start packets have empty native identities and remain rejected. It contains 44.26 seconds of near-silent received PCM; the full-stream ASR result is empty. Successful transcription processing does not turn this into a completed task.

The candidate completes the hospital, both pause and self-repair cases with the required effects and final facts. The inserted 250/1200 ms silences produce measured sample-activity gaps of 414.167/1364.167 ms, including untrimmed stem silence. Both pause cases avoid writes before the continuation; self-repair does not activate Cubbon Park or the airport.

The ambiguity case correctly asks whether Airport Road means the hospital or Starbucks and makes no premature navigation write. After “I mean Manipal Hospital, Old Airport Road,” it only performs a successful search. The spoken statement that it located the hospital is truthful, but there is no compute/start, route activation or five-minute estimate. This is a failed navigation task and a priority follow-up; a finished result handle cannot pass it.

Eleven of twelve planned non-speech probes are actually played: five control and six candidate. None produces post-probe VAD/recognition, nonempty finalized intent or extra navigation effects. Original Whitefield replies finish with the forty-minute estimate. These are false-effect controls; they do not validate acoustic empty-recognition recovery. The twelfth probe is the retained readiness failure.

## Delayed-recognition interruption

The control's delayed stop records an SDK false-interruption resume approximately two seconds after user-listening, inside the measured five-second delivery hold with one pending recognition. The old reply finishes before the stop reaches the coordinator. The candidate records an owned hold before the same SDK resume event, then an interruption request on the nonempty recognition result and release after generation cleanup. Cancellation speech completes in both cases; correct final cancellation does not erase the control's stale playback.

| Source-activity onset to receipt observation | Control native | Candidate native | Control +5 s delivery | Candidate +5 s delivery |
| --- | ---: | ---: | ---: | ---: |
| Last loud PCM in the old-reply window | 0.701 s | 0.680 s | 7.581 s | 0.699 s |
| First sustained 300 ms quiet interval begins | 0.721 s | 0.700 s | 0.781 s | 0.719 s |
| Old handle end/interruption-request event | 2.942 s | 3.450 s | 7.645 s, finished | 8.520 s, interrupted |

These values use the frozen auditor's frame-RMS activity onset and broad event-order old-reply window, including late untagged frames until the next utterance queues. Explicitly tagged old frames and publication-start timings are different measures; the [independent mechanism comparison](../results/iteration18/combined-mechanism-comparison.json) preserves them separately and reproduces all sixteen probe-window tail values. The control initially becomes quiet but later resumes, so its early quiet interval cannot pass the complete cutoff screen. The guard suppresses that resumption in the observed pair; it does not make recognition return sooner. One pair per condition, fixed order and independent native segmentation limit generalization.

## Useful-response latency and resource observations

The [post-analysis latency review](../results/iteration18/latency-review.json) retains every case. Time starts at the last meaningful spoken request's scheduled activity end: the initial command for non-speech controls, the stop request for voiced probes, or the explicit hospital answer for clarification. Silence/noise publication is not counted as speech end. Fulfilled completion requires the declared effects, final spoken facts and a finished final reply; it does not require interruption or trusted-trace success.

| Group | Fulfilled / declared cases | Completion p50 / p95, seconds |
| --- | ---: | ---: |
| Control, native probes | 3/4 | 14.149 / 21.592 |
| Candidate, native probes | 4/4 | 13.602 / 22.985 |
| Control, injected delay probes | 4/4 | 18.033 / 27.728 |
| Candidate, injected delay probes | 4/4 | 18.824 / 19.140 |
| Candidate-only language cases | 4/5 | 22.997 / 25.294 |

These are small-sample nearest-rank descriptions, with failures kept as unfulfilled rows. The ambiguity response finishes after 13.122 seconds but has no fulfilled-completion value. The report separately retains navigation-dispatch and conservatively attributed substantive-PCM times. Acknowledgment PCM latency is unmeasured because the default recorder does not tag acknowledgment audio; acknowledgment queue/finish times are not substituted for audible latency. The guard result supports avoiding resumed obsolete speech, not a general response-time improvement.

Both arms observe five asynchronous delivery holds of approximately five seconds with recognition pending throughout. Native inference remains separately recorded: one baseline initial recognition takes 10.071 seconds before its added delay. Across the two stacks, the observer retains thirty native calls: two warmups, twenty-six room recognitions and two whole-span repairs, with no dropped/pending/error observations. There is no observed probe overlap with AEC substitution.

The control/candidate memory observers retain 789/1060 samples. Minimum available system memory is 0.425/0.939 decimal GB and peak system swap is 9.953/10.831 GB. Different arm lengths, prior cases and host state prevent causal or capacity conclusions. The [control](../results/iteration18-baseline/baseline-mechanism-review.json) and [candidate](../results/iteration18-candidate/candidate-mechanism-review.json) reviews preserve 153 warning rows including client warnings, three unsampled watchdog stalls, all twenty-one server `JS_FAILED` labels, and expected process-disappearance observations at shutdown. Successful application cleanup does not erase those labels.

## Verification and reproduction

All 21 received streams were transcribed once, whole and untrimmed, after both timed stacks stopped. Literal transcripts preserve ITPL name variants such as “Itbull,” “Itbel” and “Itville”; the required Whitefield name and forty-minute facts remain explicit. This is local ASR review, not human listening. No preparation ASR/VAD selection, input retry or capture replacement was used.

The [post-inference check](../results/iteration18/post-inference-validation.json) reaches every required phase and verifies ten model references, 117 current frozen files, 117 matching snapshots, all 13 input assets and absence of all 32 recorded process IDs. Before model reads, it rejects nonterminal or missing phases, unresolved process IDs, and source or snapshot failures; terminal failed capture or ASR phases remain eligible for verification. Interruption of the verifier preserves failed status. All 21 rooms depart; both workers exit 0, with LLM termination and LiveKit exits recorded. The 721 raw decoded events match server traces; 709 are trusted and twelve packets remain rejected for missing sender identity. Full trusted equality therefore fails in eleven cases, including the startup failure.

The source/method/model freeze precedes input rendering. Independent final review receipts were saved after rendering began and explicitly record that ordering; they are not described as pre-render receipts. [Harness review](../results/iteration18/experimental-harness-review.json) and [freeze review](../results/iteration18/freeze-review.json) preserve resolved issues, exact inventories and scope. All earlier failed tests and rejected approaches remain unchanged.

Executed commands are retained with their actual arguments and deadlines in the phase receipts:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python results/iteration18/preflight.py
.venv/bin/python results/iteration18/supervise.py inputs
.venv/bin/python results/iteration18/supervise.py baseline
.venv/bin/python results/iteration18/supervise.py candidate
.venv/bin/python results/iteration18/supervise.py asr-baseline
.venv/bin/python results/iteration18/supervise.py asr-candidate
.venv/bin/python results/iteration18/audit_acoustic.py
.venv/bin/python results/iteration18/verify_after_inference.py
```

These frozen methods refuse existing output directories. A new run requires fresh output paths, rebinding the archived machine-specific references and a new preflight; it must not overwrite this evidence. Post-freeze reporting helpers are labeled separately and do not redefine the original criteria. The 696-test run includes installed SDK packages; minimal core-test dependencies may skip the optional SDK components.

The next priorities are the incomplete navigation goal after clarification, simultaneous live rooms and controlled commitment/acknowledgment comparisons. The safe rejection of anonymous packets also needs a separate startup/transport investigation. Human microphone/echo, physical speaker timing, organizer requirements and a fresh adapted FDB evaluation remain open. No PR has been published because the configured Git instructions reserve staging for the user.
