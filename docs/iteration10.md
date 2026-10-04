# Duration wording and waypoint audio failures

Iteration 9 exposed a concrete speech failure: the generated added-stop reply said 13 minutes, while the production recognizer transcribed its synthesized audio as 30 minutes. This iteration tests two general changes against that failure and nearby controls, then exercises stop addition over actual localhost audio transport. It does not tune on held-out benchmark data.

## Controlled speech comparison

The phonemizer preserves the distinction between thirteen and thirty. Writing `13` as `thirteen` produces identical phonemes and token IDs in the tested contexts. Replacing the colon before a stop's duration with “is” changes surrounding tokens; speaking speed acts later in synthesis. The [phoneme audit](../results/iteration10/phoneme-audit.json) preserves all thirteen probes and a failed metadata lookup before inference. These observations motivate the alternatives; they do not identify whether the earlier error came from pronunciation or recognition.

The frozen [protocol](../results/iteration10/number-protocol.json) declares ten texts in A/B/C/C/B/A blocks, twenty observations per arm:

- A: existing stop wording and speed 1.1.
- B: identical text at speed 1.0.
- C: “Estimated time to … is … minutes” for added/already-present stops, speed 1.1. Navigation-start controls remain unchanged.

Six added-stop texts vary only their duration: 13, 30, 14, 40, 16 and 60. The remaining controls cover a separate 13-minute navigation start, an already-present stop, airport navigation and office replacement. They are synthetic development texts, including known failures, rather than real tool effects or independent evaluation data. A saved result-replay check verifies both formatter versions produce their declared texts.

The selection rule was declared before rendering: require more correct primary durations than A without losing any case A preserves, inspect all other facts, and prefer C if both alternatives qualify because it leaves global speed unchanged. All sixty renders/transcriptions complete, including the first cold inference, without retries, clipping or exclusions.

| Arm | Intended durations preserved | Observed numeric errors |
| --- | ---: | --- |
| A, original | 18/20 | 13 → 30 twice |
| B, slower | 18/20 | 16 → 60 twice |
| C, natural wording | 20/20 | None in these texts |

**Retain C.** The production change is two stop-summary strings; configuration, planning, tool execution and speaking speed remain unchanged. All 543 Python tests pass in 5.44 seconds before inference. There are no new tests for this wording-only edit; the speech comparison supplies the relevant additional validation.

Action, replacement and no-op relationships remain represented in every transcript. All 36 applicable extra-duration observations retain zero. Proper-name spelling deviations remain in 54/60 transcripts. C adds 0.085–0.213 seconds of synthesized speech to the seven changed stop texts; the three unchanged controls add none. B adds 0.149–0.555 seconds across the ten texts while introducing the sixteen/sixty error.

The [guarded analysis](../results/iteration10/number-analysis.json) retains every row and checks exact reviewed transcripts before applying qualitative annotations. All 180 saved audio hashes and before/after source/model receipts match. All thirty same-arm repeat pairs have identical 24 kHz PCM and transcripts, so repeated observations are not independent speech samples. The 16 kHz resampled WAVs differ between repeats and remain available. This single-voice, single-recognizer proxy does not establish human intelligibility, general numerical accuracy, or which speech component caused an error.

## Live waypoint protocol

The [RTC protocol](../results/iteration10/protocol.json) declares four serial fresh rooms in waypoint/baseline/waypoint/correction order. Each waypoint case starts navigation to MG Road Metro Station, then sends “Add the nearest coffee as a stop” once initial result audio is observed. The expected effect is one Blue Tokai, Indiranagar stop on the same MG Road route, with 13 minutes total and zero extra minutes. The controls retain airport navigation and an airport-to-office correction.

The existing recorder labels the triggered second clip `correction`; here that mechanism also carries a waypoint request. The audit identifies the actual mutation as `add_waypoint` or `start_navigation`, counts successful no-ops separately from effects, and compares exact tool sequence separately from destination/stop/duration facts. Nineteen attempts and seven effects are declared. Two new first-render inputs match their complete text in a separate offline preparation check; this does not guarantee recognition after RTC/VAD processing. Earlier airport/office input WAVs are copied byte for byte.

Received-audio review is declared before launch and runs only after all owned services exit. It includes all four complete streams, with no trimming, silence removal, retries or capture-success exclusions. Synthetic input uses the same voice as output. This is an integration check, not a controlled latency comparison, human microphone demonstration or room-capacity test.

The suite finishes with **three completed captures and one failed capture**. All sixteen observed attempts finish successfully, producing six of seven declared effects. Both waypoint cases preserve MG Road, add exactly `K_BLUETOKAI`, and return 13 minutes/zero extra. The baseline airport effect succeeds. The destination-correction control times out without an office action, so its three expected follow-up calls never occur; those missing calls are not converted into successful or excluded trials.

**All three interruption checks fail:** each original result handle finishes, rather than reporting interruption. In waypoint 1, recognition of the follow-up arrives after the original reply ends. Waypoint 2 splits the same clip into “Add the nearest coffee as” and “The stop.” Whole-span re-transcription recovers the request and the correct addition executes under intent 3. The frozen checks expected intent 2 and remain failed; supplementary analysis identifies the later successful effect without rewriting them. The failed correction reaches server VAD speaking/listening states, but no partial/final transcript or completed recognition appears before the observation deadline. These logs locate the observed boundary, not the cause.

Pinned ASR reviews all four complete received streams after stack shutdown. Both waypoint recordings retain MG Road navigation and the final added-stop 13-minute/zero-extra reply, with Blue Tokai/Indiranagar spelling deviations. The baseline retains airport/88 minutes. The failed correction contains only the old airport/88-minute result: office replacement and 29 minutes are absent. Thus the content proxy supports the narrow duration change and two successful waypoint effects; it does not rescue the failed interruption or correction scenarios.

The original audit used an old handle's end timestamp as an interruption timestamp even when its state was `finished`. That analysis defect is retained in `audit.json` and corrected separately in `audit-v2.json`: end time and state remain available, while an interruption timestamp requires `interruption_requested`. Neither version converts a failed check to a pass. The corresponding original/corrected latency files remain distinct.

| Observation after initial speech end | Observations | p50 / p95 |
| --- | ---: | ---: |
| Final transcript | 4/4 | 9.192 / 12.404 s |
| Acknowledgment PCM attributed by recorder | 3/4 | 11.501 / 16.829 s |
| Successful navigation receipt | 4/4 | 17.206 / 19.351 s |
| Substantive PCM | 4/4 | 18.518 / 20.622 s |

These nearest-rank summaries retain all four initial requests; the missing acknowledgment is explicit. Follow-up values are reported per case because one input maps to a later intent and one never receives a transcript. Post-hoc mapping of waypoint 2 to its actual intent 3 gives 17.699 seconds from the scheduled follow-up speech end to substantive PCM and 25.678 seconds to handle finish. This supplements the failed frozen intent-2 check; it does not replace it. A result-handle end is not an interruption, and recorded PCM is not physical playback. The mixed fixed-order suite is not a controlled before/after latency comparison.

Full client/server event equality holds for 2/4. The other two omit initial listening from the coordinator-event view; their raw packets remain available. Every room's application cleanup finishes, every agent departs, and the worker exits 0 without escalation. These successes remain separate from the failed correction, barge-in checks and server job status. There are now 42 actual RTC captures across six separately frozen iterations, not 42 captures of this source.

The worker retains ten structured warnings: four watchdog intervals (718, 113, 132 and 436 ms), four native-handle warnings and two phonemizer warnings. The 113 ms sample is at trace-directory creation; the 436 ms sample is at synchronous `trace_f.flush()` in `agent/main.py`, during waypoint 2. Neither establishes the cause of the later failed correction. The remaining trace sink is therefore a concrete next target for off-loop logging, separate from the already-fixed FDB dispatch logger. The [supplementary log review](../results/iteration10/supplementary-log-review.json) retains all 34 warning rows across worker, LLM, LiveKit and recorders. All four server jobs are marked `JS_FAILED` on departure despite completed application cleanup. The LLM also records one unstructured `BrokenPipeError` traceback in its keepalive callback during waypoint 2 replanning; request identity and cause are not established, and it is separate from the later failed correction.

The 455 system-memory samples record a 0.392 GB minimum available, swap 5.857 GB initially / 6.805 GB finally / 9.697 GB maximum, and peak LLM/worker RSS 4.865/1.052 GB. Two final sampling errors remain. Shared pages, accelerator allocation, background applications and pre-existing swap prevent capacity or causal claims. Full source/input hashes stay fixed across the live suite, and all ten declared speech/Qwen model files match their reference bytes after all inference. The [final validation receipt](../results/iteration10/final-validation.json) records test, model, cleanup, source and delivery checks; [corrected latency analysis](../results/iteration10/latency-summary-v2.json) and [received-content review](../results/iteration10/received-audio-review/qualitative-review.json) retain their separate denominators.

## Earlier recognition delay: measured boundary

The [read-only recognition audit](../results/iteration10/recognition-delay-audit.json) revisits iteration 9's 14.195-second recognition of a 1.92-second input. Other initial recognitions took 1.372–1.499 seconds. The four complete input WAVs are identical, but exact VAD-segment PCM was not retained. No active LLM/TTS work or above-threshold received speech is evidenced in that interval; the logs cannot exclude unobserved background work.

The earlier whole-run minimum of 0.158 GB available memory occurred during startup. The slow recognition's sampled window instead has a 1.599 GB minimum. Swap rises during that window but is also high in other turns. These observations do not establish a memory, queueing or native-compute cause. A justified next diagnostic is buffered monotonic marks for queue wait, native transcription, file I/O and return scheduling, together with exact segment identity, under the same frozen conversation protocol. Keep non-reproduction as a result rather than rerunning until a stall appears.

## Reproduction and remaining work

Existing result folders reject overwrite. Use a fresh copied experiment directory and redeclare output/source hashes when repeating these historical protocols; do not rewrite saved evidence.

```bash
.venv/bin/python -m pytest -q
# Frozen speech comparison and input preparation (fresh result locations required):
.venv/bin/python results/iteration10/run_number_supervised.py
.venv/bin/python results/iteration10/run_waypoint_inputs_supervised.py
# Four live captures, then audits after owned services exit:
.venv/bin/python results/iteration10/run_suite.py
.venv/bin/python results/iteration10/audit_live.py
.venv/bin/python results/iteration10/review_received_audio.py \
  --suite-root results/iteration10 --out results/iteration10/received-audio-review
```

Human microphone/echo behavior, physical playback timing, simultaneous live rooms, a fresh adapted FDB evaluation and the organizer-issued Theme 05 brief remain unverified. The current Drive folder is inaccessible through the available authorized access path. The highest-priority remaining diagnostics are the intermittent recognition delay and server job-status ordering, followed by live cancellation/repeat visits and independent voices/names/numbers. Preserve the distinction between successful effects, complete speech, transport traces, application cleanup and server status.
