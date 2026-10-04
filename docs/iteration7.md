# End room jobs when voice sessions close

The worker previously required SIGKILL after completed conversations. Inspection of installed LiveKit Agents 1.8.3 showed that a caller disconnect closes its voice session, but does not itself end the connected agent job. Worker draining waits for those jobs. PRISM registered resource cleanup on job shutdown without connecting session closure to it.

The entrypoint now registers a session-close listener before startup and requests `ctx.shutdown` when it fires. Existing job cleanup closes the coordinator, pending UI publishing, HTTP client and trace. Cleanup start/end are logged, and a session that closes during startup no longer emits a misleading ready event. The SDK's 3,600-second drain setting and the experiment's twenty-second termination grace are unchanged. No room deletion is used.

This fits the current one-session-per-job design. It does not bound native work stuck before the close event, SDK reporting or arbitrary cleanup callbacks. The [source audit](../results/iteration7/session-job-lifecycle-audit.json) records the installed SDK files and hashes, shutdown ordering and limits. The close callback synchronously requests shutdown and returns; no session-lock cycle was found.

## Regression and live evidence

Six new lifecycle cases produced five failures and one pass before the change. They exercise the actual entrypoint with lightweight SDK fakes and the real coordinator/executor: an open conversation survives entrypoint return; participant/error/job closure triggers cleanup; repeated closure is harmless; startup closure is observed; another room stays live. The focused suite passes 32 tests and the full suite **511 tests**, completed before the audio experiment.

The frozen protocol declares eight fresh rooms in this order: baseline, correction, silence, noise, correction, baseline, noise, silence. Every room first requests the airport. Corrections request the office during observed result PCM; the 600 ms silence/noise probes occur during the result and have a fixed twelve-second observation window. Exact earlier WAVs, pinned local models and the same recorder were reused. One shared stack ran serially, with ordinary simulated route delay and no injected logging delay. All eight trials and the slower first response remain in the evidence.

| Check | Observed result |
| --- | --- |
| Captures and recorder cleanup | 8/8 completed, exit 0, no cleanup errors |
| Exact tool sequence and successful effects | 30 done attempts, 10 expected navigation effects |
| Missing completion rows or tool errors | 0 |
| Corrections | 2/2 old results interrupted; office results finished |
| Silence/noise probes | 4/4 original results finished; no later user-final event or tool call in the window |
| Complete client/server event equality | 3/8; five miss only initial listening |
| Agent departure and PRISM cleanup logs | 8/8 |
| Worker shutdown | Exit 0, no escalation; drain-log to recorded exit 0.783466 s |

Participant absence was first observed about 2.011–2.031 seconds after polling began following recorder exit, at roughly 0.5-second intervals. This is not an exact disconnect-to-departure latency. PRISM cleanup start/end spans 0.212–1.169 ms; it excludes earlier SDK teardown. Parent processes, sampled descendants, recorder PIDs and the PID identified in a sampling error were absent after shutdown. Exit receipts, room absence and cleanup logs are separate observations. The prior forced-exit run is a historical comparison with different trials and the older logger, not a controlled shutdown A/B experiment.

After all services stopped, pinned local Parakeet reviewed every complete received PCM stream, resampled without trimming, silence removal or retries. All eight ASR transcripts contain the expected core facts: complete airport/88-minute result for baseline/probes, and office navigation plus airport replacement for corrections. All have proper-name differences (`Kempegowda` → `Kempegauda`; `Manyata` → `Maniata` in corrections). These transcripts are an automatic content proxy, not human listening or an intelligibility score; they cannot distinguish pronunciation from recognition errors.

## Timing, warnings and memory

These are client transport observations from scheduled input speech end, using the recorder's declared 20 ms RMS threshold. Nearest-rank p50/p95 are descriptive: with two corrections, they are simply the smaller/larger observations. Shared warm models, fixed order and changing memory pressure prevent attribution of differences from earlier runs to either retained change.

| Speech-end to observation | Initial requests, n=8, p50/p95 | Correction requests, n=2, p50/p95 |
| --- | ---: | ---: |
| Acknowledgment PCM | 3.062 / 3.222 s | 3.039 / 3.178 s |
| Navigation-success receipt | 7.720 / 15.994 s | 10.095 / 10.150 s |
| Substantive PCM | 9.618 / 20.488 s | 13.999 / 14.180 s |

The six uninterrupted initial result handles finish at p50/p95 15.917/26.785 seconds; the two intentionally interrupted initial handles remain explicit nulls for that metric. Corrected result handles finish at 21.887/22.080 seconds. Neither handle completion nor received PCM measures physical playback completion.

Old-result non-silent PCM last arrived 760.908 and 738.766 ms after correction input onset. Scanning every frame includes 62.059 and 80.543 ms of untagged tail beyond the recorder's tagged last frame. The later coordinator handle-interruption observations, 3.525/3.617 seconds after onset, are distinct from the earlier transport stop. No physical audible-stop or latency-improvement claim follows.

The worker still logged three blocked-loop intervals: 114.3, 395.5 and 309.0 ms, all without sampled stacks. Their cause is unknown. Ten phonemizer warnings and eight native unknown-FFI-handle warnings are retained. The latter occur during SDK teardown before PRISM cleanup; identical warnings predate both this change and logging offload. They do not establish lost audio or failed cleanup, and the successful exit does not establish warning-free operation.

The 493 memory samples cover startup, serial captures and shutdown, with the final sample preceding the final service exit. System available memory fell to 0.262 GB; swap was 3.632 GB initially, 8.322 GB at the final sample and 9.131 GB at its maximum. Peak observed LLM/worker RSS was 4.894/1.395 GB. Two process-sampling errors remain recorded. These include background applications and pre-existing swap, omit the recorder/API client, and can miss short peaks; they are not allocation totals or capacity evidence.

## Reproduction and retained artifacts

The original run is `results/iteration7/`. Its protocol, preflight, source hashes, commands, runtime/model receipts, full PCM/frame/event records, every tool attempt, logs and process exits are retained. The M2 Pro/16 GiB host used macOS 15.3.1, Python 3.11.13, LiveKit Agents 1.8.3 and the same pinned models as iterations 4–5. All frozen source/input hashes matched after capture and ASR review. Inference and transport used local services; default ICE/STUN can still contact network services.

To repeat without overwriting evidence, with the recorded local dependencies, weights and free ports 7880/7882/8081/8082:

```bash
mkdir results/iteration7-repeat
cp results/iteration7/{run_suite.py,protocol.json,audit_live.py,review_received_audio.py,received-audio-review-protocol.json} results/iteration7-repeat/
cp -R results/iteration7/audio-assets results/iteration7-repeat/
.venv/bin/python results/iteration7-repeat/run_suite.py
```

The runner depends on the retained iteration 4 local-server configuration/LLM launch record, uses fresh output paths and freezes current sources. Compare those hashes to the original when claiming a repeat. Inspect `run-report.json` and all service exits before running the raw audit or offline review; do not load the review model while RTC services remain alive:

```bash
.venv/bin/python results/iteration7-repeat/audit_live.py
.venv/bin/python results/iteration7-repeat/review_received_audio.py \
  --suite-root results/iteration7-repeat \
  --out results/iteration7-repeat/received-audio-review
```

The original [raw audit](../results/iteration7/audit.json), [timing summary](../results/iteration7/latency-summary.json), [ASR review](../results/iteration7/received-audio-review/report.json), [qualitative review](../results/iteration7/received-audio-review/qualitative-review.json), [memory summary](../results/iteration7/memory-summary.json) and [process census](../results/iteration7/process-cleanup-verification.json) distinguish their own scope and failures. Earlier iteration records remain historical checkpoints.

## Decision and next work

Retain the lifecycle bridge. These eight captures also provide live integration evidence for the iteration 6 threaded logger; controlled slow-I/O evidence remains separate. Across iterations 4, 5 and 7 there are now 30 actual RTC captures, with different frozen sources and protocols.

Next priorities are substantive-response latency and memory pressure, legitimate short commands and empty-transcript false-resume behavior, active/slow-tool disconnects and simultaneous live rooms, then microphone/echo and physical-playback measurements. Fresh adapted FDB evaluation and organizer-issued Theme 05 verification remain outstanding. The supplied organizer folder could not be read because the browser's enforced access check failed; no compliance claim is made from its URL.
