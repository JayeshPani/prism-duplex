# Non-speech probes during result audio

This iteration follows the two synthetic RTC cases in [iteration 4](iteration4.md). The hypothesis was that short input without a new actionable command should not create another navigation effect, and that non-speech should not become a finalized user transcript. Any pause, resumed speech or missing result content had to be observed directly. No production agent setting was tuned during these trials.

## Frozen development protocol

Four probes were declared before generation: 600 ms of silence, a 600 ms deterministic band-limited noise burst at −18 dBFS, a 250 ms 440 Hz tone at −18 dBFS, and an exact 180 ms fragment from the beginning of the existing “Navigate to the airport” WAV. The fragment is speech, not a non-speech or ASR-hallucination test. Each case ran three times in a declared interleaved order. Every result was retained.

The driver gained `--probe`, mutually exclusive with `--correction`. It uses the existing conservative substantive-result audio trigger. After publishing the probe, it captures a fixed twelve seconds from input queue drainage, regardless of whether speech finishes, resumes, fails or changes intent. The original and current speech-handle states are recorded at that boundary. `completed_probe_observation` means the window was captured; it is not a quality score. Eleven new protocol regressions failed before implementation and pass afterward. The focused driver suite passes **29/29** and the full Python suite **491/491**.

All probes were also passed through the installed local Silero detector before live evaluation, using a fresh stream, 0.5 seconds of leading silence and three seconds of trailing silence. The silence/noise/tone maximum probabilities were approximately 0.012/0.059/0.054 with no speech-start event. The fragment reached 0.992 and produced one speech segment. No waveform, threshold or live-case selection changed in response. This direct inference check does not include RTC encoding, room history, STT or the SDK interruption policy.

The local model revisions, two-entry Qwen cache, LiveKit server and worker configuration match iteration 4. The suite hashes production source, driver, protocol and every input before startup and checks them between trials; all hashes remained unchanged. These are twelve serial repetitions of four synthetic development inputs, with warm-model and fixed-order confounds. They do not test microphones, echo, road noise, human speech, concurrent rooms or physical speaker playback.

## Observed effects and speech

All twelve captures completed their fixed windows. The complete tool log contains **36 successful attempts and twelve airport navigation effects**, one per trial. No probe caused another backend attempt or navigation write. The initial airport action had already completed before each probe.

The raw transport audit verifies all 33 frozen inputs and all recorded artifact hashes. Recorded PCM frame ranges are contiguous and cover the saved bytes; this does not prove every transmitted packet arrived. Observation windows measured 12.000152–12.002485 seconds, retaining scheduling overshoot. Complete client/server event equality holds for **4/12** captures. The other eight each omit only the initial server `agent_state: listening` event; every subsequent event matches. These remain eight failed full-trace checks rather than excluded samples.

| Probe | Live speech-start events after probe | New finalized input | Original result finished | Received speech review |
| --- | ---: | --- | ---: | --- |
| Silence | 0/3 | None | 3/3 | Airport and 88-minute ETA retained |
| Noise burst | 0/3 | None | 3/3 | Airport and 88-minute ETA retained |
| Tone | 0/3 | None | 3/3 | Airport and 88-minute ETA retained |
| Voiced fragment | 3/3 | “Nap”, “Nap”, “Now” | 0/3 | Old result truncated; two clarifications and one navigation-status reply |

After the timed suite and process cleanup, all twelve complete received PCM streams were transcribed once through the pinned Parakeet model. No samples or captured silence were trimmed, and there were no retries or exclusions. All nine controls contain the airport navigation result and 88-minute ETA. All three fragments omit the original ETA: two receive “Nap?” and a clarification; the third receives a current-navigation status response after “Now”. The status response is consistent with the completed airport effect. Proper-name spellings differ as in the earlier audio review.

This whole-stream ASR review is a content proxy, not human listening or an independent pronunciation score. The 180 ms fragment was intentionally incomplete, so these results do not establish that the recognizer invented words from non-speech. They do show how a short nonempty transcript can permanently replace an otherwise useful spoken result.

All three follow-up response handles finished. Their `current_result_finished=false` capture field means no new handle of kind `result` finished; these clarifications/status replies have kind `response`. It does not mean those follow-ups remained unfinished.

**Automatic recovery after an empty-transcript false interruption was not exercised.** The nine non-speech controls did not trigger speech activity, while all three speech fragments produced nonempty transcripts. Do not describe these results as a successful false-interruption-resume test. No short-utterance filter was added: such a rule could also suppress legitimate brief commands and requires independent acceptance tests.

## SDK boundaries and runtime issues

The installed LiveKit Agents 1.8.3 source has a 0.5-second interruption threshold, while PRISM's Silero speech-start minimum is 0.05 seconds. False-interruption resumption defaults to a two-second timer after detected speech end, which a pending turn decision may extend. Empty final transcripts are filtered before the final-input hook; a nonempty final can permanently interrupt paused speech. Current PRISM telemetry does not forward the SDK's explicit false-interruption/resumed event, so state transitions and received PCM provide only indirect evidence of that path. Pausing can discard queued audio; the same handle resuming does not by itself prove lossless content. Exact installed-source paths, lines and hashes are in `sdk-interruption-audit.json`.

The live worker also logged a **613 ms event-loop stall**, with a sampled stack inside the synchronous tool logger’s snapshot replacement. This is a watchdog interval, not a measurement proving that replacement alone consumed the entire interval. The watchdog attributed only 4 ms to CPU time during that interval. This is direct evidence that telemetry I/O can delay audio and turn handling; it does not prove that the stall caused any particular recognition or interruption outcome. Moving this work off the event loop must preserve the current contract: save the attempt before entering a backend, prevent dispatch if logging fails, check intent again after any new await, and retain cancelled/uncertain attempts. Unawaited background logging would violate that contract.

The suite's shutdown is a separate limitation. The worker entered SDK draining with a logged **3,600-second** timeout. It did not exit within the runner's twenty-second grace period and was terminated with SIGKILL. The model server received SIGTERM and LiveKit exited normally; all three owned PIDs were subsequently absent. This is not a clean worker shutdown or proof of an indefinite SDK hang. The runner's deadline is shorter than the SDK's configured drain window. Its forced termination and native channel-close messages remain in the artifacts.

Memory sampling covered startup and the captures, including descendants of each owned stack process but excluding the independent recorder client. Across 628 samples, available system memory fell to 0.545 GB; system swap began at 4.935 GB, ended at 8.191 GB and peaked at 8.696 GB. Sampled parent RSS peaks were 4.872 GB for Qwen, 1.650 GB for the worker and 0.059 GB for LiveKit. These decimal GB figures include pre-existing system load, omit unsampled peaks and do not establish a total allocation or sustained-capacity bound. Do not sum overlapping RSS/unified-memory measurements.

## Reproduction and retained evidence

Use the local model/server setup from [iteration 4](iteration4.md), with a fresh worker manifest and output paths. A single new noise capture uses:

```bash
python scripts/local_livekit_audio_experiment.py \
  --input results/iteration5/audio-assets/navigate.wav \
  --probe results/iteration5/audio-assets/noise.wav --probe-observation-seconds 12 \
  --out results/iteration5/new-noise-capture \
  --worker-manifest results/iteration4/new-live-run/worker-manifest.json \
  --protocol results/iteration5/protocol.json --ready-timeout 45 --response-timeout 60
```

The suite helper is an exact same-host experiment runner: it uses the recorded local model launch command and refuses an existing stack directory. To repeat all twelve trials without replacing prior evidence, first stop task-owned services and copy only the frozen inputs and helper into a new sibling directory:

```bash
mkdir results/iteration5-repeat
cp results/iteration5/run_suite.py results/iteration5/protocol.json results/iteration5-repeat/
cp -R results/iteration5/audio-assets results/iteration5-repeat/
.venv/bin/python results/iteration5-repeat/run_suite.py
```

The helper checks loopback ports, records launches, enforces per-trial deadlines, continues after individual capture failures and cleans up its owned processes. Fatal setup/stack failures remain distinguishable from attempted captures; compare against the full declared order. Its existing twenty-second worker cleanup limit reproduces the limitation above. A different host must first prepare its own model launch record instead of treating these absolute paths as portable configuration.

Whole-stream recognition can be repeated into a fresh directory:

```bash
.venv/bin/python results/iteration5/review_received_audio.py \
  --suite-root results/iteration5 --out results/iteration5/new-received-audio-review
```

`results/iteration5/` retains the protocol, generated bytes and hashes, direct VAD events, driver regressions, all twelve raw captures, shared tool/server logs, model receipts, process cleanup and memory samples. `received-audio-review/` contains every later transcript and its separate protocol/provenance. No failed prior experiment was replaced.

[The complete raw audit](../results/iteration5/probe-audit.json) retains every capture's event coverage, tool pairing, PCM byte checks, input hashes and follow-up states. Its derivation is in `results/iteration5/audit_probes.py`; it loads no models.

## Next iteration

Prioritize the measured telemetry stall with cancellation and dispatch-order fault injection, then configure and measure a bounded worker drain. Add explicit false-resume telemetry before claiming recovery behavior. Further interaction evidence should include an actually exercised empty-transcript pause/resume path, legitimate short commands, pauses, slow tools, noisy human input and simultaneous rooms. Physical microphone/speaker timing, a fresh adapted FDB run and organizer-issued Theme 05 verification remain outside this evidence.
