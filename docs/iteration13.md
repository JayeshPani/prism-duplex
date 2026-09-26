# Preserve recorder sender identity through participant lookup

Iteration 12 lost session readiness in one recorder: its raw packet journal contained `session_started`, but the SDK exposed no participant and the recorder never published a request. The saved public packet cannot show whether the native identity was blank or merely unresolved, so that historical capture remains a failure.

The recorder now preserves the transport sender identity while the installed SDK invokes its synchronous data callback. It retains unresolved packets in a bounded queue and delivers them only when that exact identity appears in the room map as an agent. It drains older packets from that sender before later packets, copies immutable payload bytes, and records original receipt time separately from attribution time. Blank identities, non-agents, overflow and unresolved shutdown entries have explicit dispositions. It does not infer identity from payload content or a later sole agent.

The adapter delegates the SDK handler unchanged, preserving its native-buffer copy/disposal and callback behavior. It is limited to the reviewed `livekit` 1.1.18 `room.py` source hash; other source versions fail before connection until reviewed. This is an experiment recorder change. Production agent behavior, models, turn settings and speech policy are unchanged.

The [protocol](../results/iteration13/protocol.json) declares four fresh rooms in waypoint/baseline/waypoint/correction order, with the same four input WAVs and no artificial participant delay, retries or exclusions. All received audio is reviewed after the timed stack has stopped, using the [previously declared whole-stream method](../results/iteration13/received-audio-review-protocol.json). A live run without delayed attribution demonstrates compatibility, not reproduction of the earlier readiness failure.

## Verification

The focused recorder suite passes 41 tests. The full suite passes **567 tests in 5.24 seconds**, including delayed readiness/listening events, exact-identity trust boundaries, FIFO/exactly-once promotion of each received packet, bounds, original timestamps, nested/error restoration and the actual installed SDK handler. The SDK-specific test skips when the optional RTC dependency is absent; it ran in this environment. Duplicate transport deliveries are distinct packets, not a claim of network exactly-once delivery.

The initial full-suite/preflight receipts are preserved with `.initial` suffixes. Before any live service launched, the SDK boundary test was made optional for the lightweight test environment, the full suite was rerun, and a fresh preflight reverified source/input/method hashes and all ten model reference files. No recorder or production changes occurred between those preparations. The launcher requires that successful preflight before creating the stack.

## Live results

The [four-case audit](../results/iteration13/audit.json) verifies **4/4 completed captures, 19/19 completed tool attempts and all seven expected route effects**. Both coffee-stop additions preserve the MG Road destination and 13-minute estimate; the office correction replaces the airport and retains 29 minutes. The airport baseline completes with 88 minutes. All three requested original-result interruptions occur. No extra, failed or unfinished tool attempts appear in this suite; historical failures remain unchanged.

The [independent attribution audit](../results/iteration13/supplementary-log-review.json) finds all 192 raw packets already have matching public-participant and native identities. All are promoted immediately; none is buffered, rejected or unresolved. Their original receipt timestamps remain intact, and all four promoted and raw event streams match the server traces. **The live run does not exercise delayed attribution.** Component tests establish the repaired lookup boundary; this integration run establishes compatibility only. It cannot retrospectively assign the iteration 12 anonymous packets to an agent.

The [whole-stream review](../results/iteration13/received-audio-review/qualitative-review.json) transcribes every received stream once, untrimmed, after all timed services stop. All four contain their requested final facts, with disclosed name variants: Blue Tokai/Indiranagar become “blue Takeai in Durinegar,” Manyata becomes “Maniata,” and Kempegowda becomes “Kempegauda.” Both stop replies retain “thirteen minutes, zero extra minutes”; baseline and correction retain 88 and 29 minutes. Only truncated old-result wording remains in interrupted cases. These are local ASR observations, not human listening or general pronunciation accuracy. All twenty raw/resampled file hashes and complete-stream durations agree.

| Scheduled speech-end to client receipt | Initial requests, n=4, p50/p95 | Follow-ups, n=3, p50/p95 |
| --- | ---: | ---: |
| Transcript | 1.781 / 2.058 s | 1.749 / 1.772 s |
| Acknowledgment PCM | 3.123 / 3.341 s | 3.221 / 3.240 s |
| Mutation-success receipt | 7.876 / 16.546 s | 6.197 / 9.969 s |
| Substantive PCM | 8.844 / 17.562 s | 7.180 / 11.459 s |

The [latency derivation](../results/iteration13/latency-summary.json) uses the same nearest-rank calculation and keeps all seven turns. The single uninterrupted initial result finishes after 14.574 seconds; the three intentionally interrupted initials have no completion time. Follow-up completion is 15.163/19.209 seconds p50/p95. Last old non-silent PCM arrives 658.727, 639.830 and 739.120 ms after follow-up onset, while handle interruption is recorded after 3.252, 3.196 and 3.528 seconds. The audit retains 40.057/80.162 ms of untagged late PCM in the latter two cases. Physical playback, handle state and client-received PCM are different boundaries. Seven mutation-success-to-result-PCM intervals have 1.016/1.491 seconds p50/p95. This small, fixed-order reused development suite is not a controlled latency improvement.

## Recognition, warnings and cleanup

The unchanged diagnostic observer retains eight native calls: one warmup plus seven recognitions. All [80 stages and 16 PCM files](../results/iteration13/stt-stage-independent-audit.json) verify; eight submitted PCM hashes are distinct and each equals the actual written WAV payload. No observation errors, drops or pending calls remain. Recognition ranges from 0.932 to 1.374 seconds. The slowest call includes 0.994 seconds in generation, 0.333 seconds in audio loading and 0.116 ms from submission to worker entry. This does not explain or erase iteration 12's 6.853-second call: exact segment bytes and runtime state differ. Host-call timings preserve lazy execution and are not exclusive accelerator time. Measured helper overhead totals 2.348 ms, maximum 0.396 ms; uncounted wrapper work remains.

All four application cleanup callbacks finish, agents depart, and the worker exits 0 without escalation. All 192 accepted trace events are flushed with zero rejection. Server jobs still receive four `JS_FAILED` labels, consistent with the separately investigated SDK/server completion boundary; no upstream fix is claimed. Seven stack/client PIDs and the later ASR process are absent after their respective exits.

The complete log census retains **33 warnings**, including two unsampled watchdog stalls of 112.1 and 658.1 ms, four worker FFI warnings and three phonemizer warnings. There are no runtime error/traceback rows. AEC observation sees 72 substituted frames (3.6 seconds), all during initial acknowledgments, with no follow-up overlap. The 273 memory samples reach 0.401 GB available; swap starts at 5.131 GB, ends at 8.998 GB and peaks at 9.608 GB. One terminal worker sampling error remains alongside its later absence check. These observations do not establish the cause of stalls or a live-room capacity guarantee.

## Reproduction and decision

Retain the recorder change with its pinned SDK compatibility check and explicit attribution limits. The live result supports compatibility; deterministic tests support delayed lookup handling. Leave previous failures and their original records intact. All ten model reference files and forty frozen run files match their post-inference hashes. Exact evaluated production, recorder, observer and test sources are preserved in the [70-file snapshot](../results/iteration13/source-snapshot.json).

Commands recorded for this run, from the repository root:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python results/iteration13/preflight.py
.venv/bin/python results/iteration13/run_suite.py
.venv/bin/python results/iteration13/run_received_audio_supervised.py
.venv/bin/python results/iteration13/audit_live.py
.venv/bin/python results/iteration13/summarize_latency.py
.venv/bin/python results/iteration13/analyze_stt.py
.venv/bin/python results/iteration13/audit_stt_stages.py
.venv/bin/python results/iteration13/audit_supplementary.py
.venv/bin/python results/iteration13/review_received_facts.py
```

These frozen paths intentionally refuse overwrite. A new capture needs a fresh output tree, copied scripts/inputs and a newly declared protocol/review method with refreshed hashes. Restore source snapshots to their original repository-relative locations. Rebind machine-specific paths in **copies** of the receipts: preflight reads absolute model paths from iteration 11, and the launcher reads the absolute Python/model command from iteration 4 plus the Homebrew LiveKit server location. Point the new scripts at those copies and create a fresh tested-source receipt; do not edit historical receipts. Runtime/model pins and original commands are retained in the linked manifests. These historical scripts are not an unmodified fresh-checkout installer.

Next priorities are a controlled replay of exact recognition segments to isolate generation variance, human microphone/echo and physical playback acceptance, live cancellation/repeated visits and concurrent rooms, and a fresh separately reported adapted FDB evaluation. Organizer Theme 05 verification still requires accessible organizer text. PR publication requires user-selected staging under this workspace's Git instructions; no files were staged or committed by this iteration.
