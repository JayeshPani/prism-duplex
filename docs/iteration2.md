# Reliability iteration 2 — 2026-09-25

This iteration closes additional failures found after the [first baseline comparison](reliability.md). The first checkpoint below uses scripted planners or mocked model/load interfaces. The later speech validation section records actual local Kokoro and Parakeet inference. Live microphone interaction, acoustic latency, and held-out benchmark performance remain unverified.

## Changes and evidence

| Hypothesis | Failure reproduced before the change | Retained change and verification |
|---|---|---|
| Stream completion must understand JSON strings | 8/9 parser tests failed; braces inside a quoted reply could prematurely stop the stream | Parse complete JSON objects, reject duplicate keys/non-finite values and prose wrappers, preserve reasoning/fence handling, and close cancelled streams; 9/9 pass |
| Uncertain places must not silently become destinations | 18/67 place cases failed, including ambiguous and unknown destinations | Accept exact IDs/names/aliases or unique whole-word partial names; return clarification candidates without selectable search results; 67/67 pass, plus an explicit follow-up case |
| One invalid call must invalidate the entire plan | 38/44 resolver cases failed; five integration cases dispatched a valid write beside a malformed sibling | Reject malformed calls, argument objects, unknown tools, missing/placeholder required arguments, and invalid structural types before returning any plan; 44/44 pass |
| Session cleanup must own every outstanding task | Eight lifecycle cases failed, plus a separate replaced-resolver case | Retain superseded task ownership, consume/report exceptions, ignore late closed-session events, bound cancellation-resistant shutdown, and recheck intent before speech |
| A spoken-summary failure must remain observable without rewriting successful effects | Four current/superseded responder failure cases failed | Emit a separate summary error; say “I couldn't prepare a spoken summary of the result.” only for the current intent; keep tool success recorded |
| Declared model revisions must reach the actual loader | Baseline loaders accepted mutable Hub names; requesting a Hub name from MLX can bypass the server's configured snapshot | Resolve exact Qwen/Parakeet snapshots, forward paths to loaders and servers, use MLX's `default_model` request alias, reject unfrozen benchmark profiles, and distinguish selection from load receipts; 32 focused provenance/manifest tests pass |

Before/after logs are under `results/iteration2/`: `llm-stream-*`, `place-clarification-*`, `resolver-*`, and `coordinator-*`. The focused coordinator/clarification run passes 126 tests. The baseline logs cover successive isolated failures; their counts must not be added to the full-suite total as independent observations. Place clarification remains a deterministic map lookup policy, not measured ASR confidence or geocoding accuracy.

## First integrated checkpoint

- **249 Python tests passed** in 3.69 seconds (`final-unit.txt`).
- **6 frontend tests passed**, and the Next.js production build passed (`frontend-tests.txt`, `frontend-build.txt`).
- Python compilation, shell syntax, frozen local configuration preflight, and whitespace checks passed.
- The unchanged seven-scenario development runner, with two policies and five repeats, passed **70/70** again: **110/110 intended actions**, **zero stale dispatches**, and **zero timeouts**. All ten writes already dispatched before correction still completed and remain recorded.

The original source passed 40/70 in the same constructed scenario set. Repeating these scenarios checks integration; it does not add independent conversational examples. Raw current traces and source hashes are in `results/iteration2/scripted/`, and aggregates are in `scripted-run.txt`. Earlier iteration results remain preserved under `results/reliability/`.

| Policy | Dispatch p50 / p95 | Effect completion p50 / p95 |
|---|---|---|
| Fixed 40/40 ms | 42.549 / 85.146 ms (115 attempts) | 84.786 / 116.925 ms (55 effects) |
| Read/write 20/60 ms | 63.159 / 106.759 ms (115 attempts) | 104.792 / 136.837 ms (55 effects) |

These timings start at finalized text and use simulated 40 ms route computation. They exclude recognition and audio playback. They do not justify a new production commit threshold. The first iteration's browser trace-import and map-clear checks still apply to unchanged frontend behavior; the production build and frontend unit checks were rerun here.

## Model provenance limits

Qwen and Parakeet have full immutable Hub revisions in `agent/config.yaml`. Changing `PRISM_LLM_MODEL` clears the inherited revision and requires `PRISM_LLM_REVISION`, unless explicitly opting into unfrozen development. Benchmark preflight rejects that opt-out. Agent STT and evaluation ASR now use pinned local paths; CUDA evaluation preserves upstream decoding while pinning the checkpoint, and Mac evaluation remains an adapted ASR path whose equivalence is unverified.

The benchmark script starts its own server from the selected snapshot and rejects an already responding endpoint. A selected directory is not proof of successful server loading or inference. STT load receipts are written only after a loader returns; they still do not attest that inference occurred. Kokoro's release has no publisher digest, so locally observed hashes are identification evidence only.

Mac inference dependencies were installed (MLX 0.32.2, MLX LM 0.31.3, Parakeet MLX 0.5.2), and model downloads were started. Download completion and usable weights are not established by these test results. No server inference or combined STT/LLM/TTS capacity measurement was completed at this checkpoint. LiveKit credentials and benchmark audio remain unavailable.

## Reproduce and continue

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/reliability_experiment.py --out results/iteration2/rerun --repeats 5
.venv/bin/python scripts/capture_run.py --validate-local-only
cd web
node --experimental-strip-types --test lib/events.test.ts
npm run build
```

Runtime, configuration, source hashes, and validation commands for this checkpoint are recorded in `results/iteration2/validation.json`. Reproduction assumes the dependencies described in the README. Before inference, complete `bash scripts/download_models.sh local-mac` and launch MLX with the resolved directory shown there. For an adapted audio smoke run, supply LiveKit credentials and use `PRISM_PROFILE=local-mac JUDGE=none LIMIT=10 bash scripts/run_benchmark.sh`.

Next priorities are real local inference and concurrent model memory/latency, independently labeled clarification audio, measured audible interruption behavior, and backend reconciliation for unknown writes. The in-memory ledger and bounded cancellation cannot undo already dispatched external effects. No new benchmark scenarios or answers informed this iteration.

## Follow-up: actual local speech inference

Once Kokoro and the pinned Parakeet download completed, the first real TTS smoke test exited inside eSpeak before producing audio (`results/iteration2/tts-smoke.txt`). The resolved data directory was 162 UTF-8 bytes long. eSpeak NG 1.52 uses a [160-byte POSIX path buffer](https://github.com/espeak-ng/espeak-ng/blob/1.52.0/src/libespeak-ng/speech.h#L62); its [path lookup truncates the directory](https://github.com/espeak-ng/espeak-ng/blob/1.52.0/src/libespeak-ng/speech.c#L230). Phonemizer resolves symlinks, so a short alias does not fix the long resolved path.

`KokoroTTS` now copies an overlong bundled data path into a short temporary directory, verifies its length before native initialization, and keeps it for the model's lifetime. Short installations use their original data directory. A native phonemizer probe confirmed the diagnosis, eight regressions cover path/copy/lifetime behavior, and the complete TTS→resample→STT run now succeeds. The full Python suite after this follow-up passes **274 tests** (`post-speech-unit.txt`). The synthetic runner also now closes its resources on success, timeout, cancellation and setup failure; invalid concurrency and empty selections fail clearly, and reports retain raw events.

Four independently declared sentences were generated with local Kokoro `af_heart`, speed 1.1, converted from 24 kHz to 16 kHz with the LiveKit resampler, and recognized by the actual pinned MLX Parakeet model. Inputs and expected words were fixed before running; no failed or cold trial was discarded.

| Input case | Audio duration | TTS time | STT time | Exact normalized words |
|---|---:|---:|---:|---|
| Navigate to Maple Library | 1.685 s | 1.124 s | 2.895 s | Pass |
| Brookfield Station → Cedar Market correction | 3.520 s | 1.076 s | 0.321 s | Pass |
| Navigate to Riverside | 1.429 s | 0.470 s | 0.103 s | Pass |
| West Harbor Garage, level two | 2.368 s | 0.659 s | 0.122 s | “two” → “2” |

All **4/4** inputs completed; **3/4** matched exactly under the declared lowercase/alphanumeric token normalization. There was one word edit among 25 reference words (**4% WER**). The last mismatch is numeric formatting, preserved in the score rather than retrospectively normalized away. This tiny same-voice synthetic sample is neither a human-speech accuracy estimate nor a navigation task score.

Model loading took **0.636 s for TTS** and **17.890 s for STT**. The first recognition took 2.895 s; later recognition calls took 0.103–0.321 s. Do not hide the first inference inside a warm-only summary. Peak process RSS was **1,661,878,272 bytes** and separately observed MLX peak allocation was **2,636,011,062 bytes**. These are overlapping accounting measures and must not be added. Both speech models were loaded together, but the pipeline stages ran sequentially; combined LLM/STT/TTS concurrency is not yet measured.

Evidence: `results/iteration2/local-speech-run1/report.json` contains all transcripts, times, model revisions, file hashes, load receipts, and errors. Original 24 kHz and resampled 16 kHz WAVs remain in its `raw/` folder; `generated-audio.zip` packages all eight WAVs for sharing, with hashes in `audio-archive.json`. This run uses no LiveKit server, microphone, VAD, echo cancellation, interruption, or LLM. It demonstrates local speech inference and fixes an actual startup failure; it does not establish full-duplex behavior.

```bash
.venv/bin/python scripts/local_speech_experiment.py --profile local-mac --out results/local-speech/new-run
```

The harness requires already downloaded weights, forces offline Hub access, and refuses to overwrite an existing output directory. Qwen's download remains in progress at this checkpoint. When its local server is ready, `scripts/local_navigation_experiment.py` runs four predeclared navigation cases through the real planner and simulated tools, preserving all raw responses and attempts.

## Follow-up: first-speech warm-up and artifact integrity

The first speech run motivated a controlled warm-up experiment. Four fresh STT-only processes ran in the declared order baseline, warm, warm, baseline. Warm trials recognized one second of silence before the same saved phrase. Model files and source hashes were verified, every output was retained, and each process had a 120-second deadline.

| Trial | Mode | Model load | Silence warm-up | First actual speech |
|---|---|---:|---:|---:|
| 1 | Baseline | 0.912 s | — | 0.963 s |
| 2 | Warm | 0.543 s | 0.646 s | 0.112 s |
| 3 | Warm | 0.657 s | 0.329 s | 0.105 s |
| 4 | Baseline | 0.538 s | — | 0.395 s |

All four speech transcripts matched “Navigate to Maple Library.” Both warm-up transcripts were empty. The mean first-speech time was 0.679 s without warm-up and 0.108 s with it, but **two trials per group do not establish a general speedup**. Shared OS/kernel caches, pre-run asset hashing, fixed order and competing workloads remain confounds. The cost moves into startup; it is not eliminated. These fresh-process controls, rather than the earlier 2.895-second first inference, support the comparison. Raw trials, memory, hashes and the prespecified procedure are in `results/iteration2/stt-warmup/`.

Retain a one-second silence warm-up for the tested MLX backend in `agent.main.prewarm`. CUDA startup remains unchanged pending hardware validation. Warm-up bypasses conversation segment storage. A subsequent **actual production prewarm** loaded Silero, Parakeet and Kokoro, primed speech inference and synthesized “Ready.” in **3.548 s**; the first saved phrase then transcribed correctly in **0.194 s**, with zero retained user segments. This is startup/recognition evidence, not a live room test (`results/iteration2/prewarm-smoke/result.json`).

Kokoro loading and the downloader now reject bytes that differ from the reference hashes observed in the successful speech run. The reference origin is explicit; `source_verified` remains false because no publisher digest was available. The original speech report is unchanged. Its later `source-manifest.json` explicitly identifies itself as a post-run capture taken before checksum-enforcement edits. All eight archived WAVs match their recorded hashes; saved PCM level checks found no clipped samples, without claiming a perceptual listening assessment.

The full Python suite now passes **278 tests** (`prewarm-unit.txt`). Navigation harness tests additionally verify that a task from an old intent keeps its originating turn after a correction, so a correct final destination cannot hide a stale dispatch.

```bash
# Restore the original generated WAVs if using only repository artifacts:
unzip -n results/iteration2/local-speech-run1/generated-audio.zip -d results/iteration2/local-speech-run1
.venv/bin/python scripts/stt_warmup_experiment.py --out results/stt-warmup/new-run \
  --source-report results/iteration2/local-speech-run1/report.json
```

The warm-up runner resolves the declared model revision in the current machine's cache and verifies the original content hashes; it does not require the original author's absolute cache path.

## Follow-up: dispatch evidence and effective benchmark speech

The benchmark runner reads tool telemetry once. A blocked backend previously had **zero visible records until it returned**, so a slow or crashed invocation could disappear from evaluation. The live agent now persists a dispatch intention after the final intent check and before backend entry. Completion updates that same record. An unfinished attempt has `status: unconfirmed` and omits `timestamp_end`; a null end would break the upstream timestamp arithmetic. Atomic snapshots and a stable cross-process lock preserve concurrent workers' rows while readers see a complete snapshot.

This is an intention record, not proof of backend execution or exactly-once effects: a crash between persistence and backend entry can overcount an attempted action. A finalized end timestamp means executor observation ended, including cancellation or an unknown outcome. Start-log failure prevents dispatch, releases the write reservation for retry, and emits an explicit logging error. Completion-log failure preserves the unconfirmed intention and the actual backend outcome. Reused reads, blocked retries, and calls rejected by the final intent check add no dispatch row. Process-exit persistence is tested; power-loss recovery and noncooperating external writers are not validated.

**45 targeted tests passed**, including 14 new dispatch cases that execute the unmodified upstream collector, check visibility during a blocked call, prevent duplicate completion rows, force a process exit inside a backend, and preserve 24 concurrent-worker attempts alongside an existing legacy row. Evidence and source hashes are in `results/iteration2/dispatch-evidence.json`, with before/after logs in `dispatch-before.txt` and `dispatch-after.txt`. Upstream scoring still uses function names and arguments without checking confirmation status; a tool-call pass does not establish a successful effect.

The compatibility snapshot rewrites retained history synchronously. A local file-I/O experiment used 20 start/finish pairs per initial log size and nearest-rank percentiles; the log grew by one row per pair.

| Initially retained rows | Start persistence p50 / p95 | Completion update p50 / p95 |
|---|---:|---:|
| 0 | 0.32 / 0.52 ms | 0.35 / 0.46 ms |
| 100 | 0.83 / 1.10 ms | 0.93 / 1.32 ms |
| 1,000 | 3.78 / 3.94 ms | 3.88 / 4.43 ms |
| 10,000 | 36.27 / 41.35 ms | 35.75 / 41.04 ms |

Raw samples are in `results/iteration2/dispatch-io-cost.json`. These are telemetry costs, not conversational latency. Per-run benchmark isolation bounds retained history; long-lived deployment needs an archival/export design that bounds synchronous work without discarding attempts.

Benchmark mode now follows the configured result-speech setting, which defaults to enabled. `PRISM_BENCH_SPEAK_RESULTS=0` is an explicit benchmark-only silence ablation; `1` enables speech, invalid values fail, and Assistant/car modes ignore this override. Capture manifests record the effective room mode, coordinator settings, and silence-ablation flag alongside the original configuration and environment. Configuration and manifest regressions verify that recorded behavior matches runtime selection. The integrated Python suite at this checkpoint passes **303 tests in 4.64 seconds** (`results/iteration2/dispatch-integration-unit.txt`); this does not constitute a live benchmark or audible-response validation.

## Follow-up: first actual Qwen navigation run and diagnosed failures

The pinned local Qwen3-8B-4bit server completed all four predeclared navigation cases, passing **2/4**: correction before commitment and clarification followed by an explicit destination. The run retained **18 backend attempts and six effects**, with no runtime errors or timeouts and two backend error outcomes. These are small development cases with simulated navigation tools, not held-out examples or Full-Duplex-Bench results. A passing effect/response check does not establish that every spoken-text claim is accurate.

| Observed failure | Evidence and implication |
|---|---|
| Returning to the airport only computed a route | The third plan omitted `start_navigation`; no activation was attempted, so the backend correctly kept the office route active. Confusion between historical outcomes and a fresh request is a hypothesis about model behavior, not proved reasoning. |
| “Blue Tokai in Indiranagar” unnecessarily required clarification | Lookup rejected the spoken connective despite the complete stored name “Blue Tokai, Indiranagar.” The intended waypoint was absent; the active route was preserved. |
| Cancellation was announced before dispatch | The planner's “Navigation has been cancelled.” was delivered as plan narration before the backend ran. Separately, the resolver copied planner narration into the missing `repaired` field, causing summaries to receive “Computing the route…” instead of the original navigation request. |

The [first-run report](../results/iteration2/local-navigation-run1/experiment.json), all four JSONL traces, [semantic review](../results/iteration2/local-navigation-run1/review.json), server log and listener/launch observations are preserved. They include raw planner/responder requests and outputs, source hashes, attempted calls and final state. **This run included the server's first actual inference request**; earlier health checks performed no inference, and no cold request or failed case was discarded. Timings begin at finalized text and end at text delivery or simulated dispatch/effect, with no microphone, STT or TTS. The controlled launch selected the recorded immutable snapshot; this is not independent process attestation.

The combined follow-up clarifies the car-domain instruction that every explicit navigation request—including a return—requires both route computation and activation. Lookup accepts internal “in”/“at” only when removing them identifies a complete stored name; ambiguous matches still require clarification. Tool-plan narration remains in trace events but is no longer spoken or added to conversation history. No replacement phrase is injected; acknowledgments, no-call replies, clarifications and grounded result summaries remain. An absent or empty `repaired` value now leaves summaries to use the original utterance, while explicitly supplied repaired text is preserved.

Eight connective regressions failed before the lookup change; the full lookup suite then passed **82/82**, with **107 related tests** passing. Eight speech-truthfulness regressions failed before their change, then passed within **87 related tests**, including stale-response and lifecycle checks. Logs are `place-connectives-{before,after}.txt`, `place-connectives-related-tests.txt` and `speech-truthfulness-{before,after}.txt` under `results/iteration2/`. The integrated checkpoint passed **325 Python tests in 4.58 seconds** (`planner-integration-unit.txt`). These changes are evaluated together, so a subsequent improvement cannot be causally attributed to any single fix. No second-run result is claimed at this checkpoint.

## Follow-up: real local planner results and remaining failures

The second Qwen run passed **4/4 declared navigation effect checks**, with **20 attempts, eight intended effects, no timeouts and no runtime errors**. The one backend error was the expected ambiguous-place lookup. The airport return now activates its new route, the waypoint is added, cancellation clears it, and clarification precedes the explicit Airport Road destination. All original failures remain in `local-navigation-run1/`; the revised traces and report are in `local-navigation-run2/`.

**The effect score is not a complete semantic pass.** Review of all 19 delivered texts and 20 attempts found that the named waypoint became `find_nearby(category="coffee", max_detour_min=5)`: the user had supplied neither a category-only request nor a five-minute limit. It happened to select Blue Tokai on this fixed route. This run therefore does not exercise the new connective lookup path; its unit regressions provide that evidence. The model also still generated premature or incorrect plan narration, which was retained in traces and suppressed from speech. Delivered result text matched observed outcomes; one reply unnecessarily read route ID R3. See `local-navigation-run2/review.json` for the full review and trace references.

| Final-text latency, second run | n | p50 | p95 |
|---|---:|---:|---:|
| Acknowledgment text | 10 | 0.703 s | 0.882 s |
| First substantive response text | 9 | 15.111 s | 18.105 s |
| Backend dispatch | 20 | 10.782 s | 13.979 s |
| Intended effect completion | 8 | 11.723 s | 15.149 s |

These are small descriptive samples, not stable percentiles or acoustic latency. The superseded first intent needs no substantive response, hence nine response turns across ten submissions. The first run's substantive p50/p95 was 10.536/17.006 s while omitting two intended effects; this follow-up establishes no speedup. Server/cache warmth, differing plans, the combined changes and ordinary background activity prevent a controlled latency comparison. This M2 Pro configuration is still too slow for a convincing real-time response claim.

A separate fresh run of the unchanged 24 synthetic development scenarios scored **17/24** using the conservative scorer. All **34 tool attempts** completed successfully, with no tool errors or scenario timeouts. Five failures involved representation/contract differences: four ordinal date spellings (three also supplied grounded `flight_id` keys absent from the exact expectation) and “black card” versus “black.” Two failures were substantive: an invented product price ceiling and an omitted neighborhood-filter action. The 17/24 score remains unchanged; semantic review does not relabel failures as passes. All cases, events, configuration, source hashes and review are in `local-synthetic-run1/`. This tool-only harness deliberately disables result speech and uses instant mock tools; it does not assess spoken answers or audio latency.

The final scripted integration rerun still passed **70/70**, completing **110/110 intended actions** with zero stale dispatches; all ten already-started superseded writes remain recorded (`scripted-after-planner/`). This repeats existing development cases, not independent evidence. The final Python suite passed **343 tests in 4.70 s** (`final-local-model-unit.txt`). Frontend code is unchanged from the earlier six-test/build/browser checkpoint.

## Follow-up: concurrent local model capacity probe

On the same 16 GB M2 Pro, one shared STT queue and one shared TTS queue served 1, 2 and 4 requested rooms while the task-owned Qwen server handled planner requests. Each room independently transcribed the same saved synthetic phrase, planned an office route and synthesized “Your route is ready.” All **21/21 component operations** completed and passed their declared checks. STT matched normalized words, planners produced the required dependency chain without extra arguments, and TTS produced nonempty PCM. No route was executed and synthesized speech content was not semantically scored.

| Requested rooms | Batch wall time | STT range, including queue | Planner range, including queue | TTS range, including queue |
|---|---:|---:|---:|---:|
| 1 | 11.555 s | 3.047 s | 11.550 s | 1.550 s |
| 2 | 6.734 s | 1.067–1.251 s | 3.471–6.700 s | 0.744–1.280 s |
| 4 | 19.191 s | 2.591–3.164 s | 8.303–16.239 s | 2.865–3.861 s |

STT/TTS loading took 1.920/0.875 s. Speech-process lifetime peak RSS was **925,614,080 bytes**; separately observed MLX peak allocation was **2,075,792,254 bytes**. Maximum sampled server RSS was **4,745,838,592 bytes**. RSS and MLX allocations overlap and do not describe total unified-memory pressure; do not add them or infer spare system capacity. The probe samples memory every 250 ms. Raw measurements, all requests/outputs, model receipts and source hashes are in `local-overlap-run1/report.json`; `raw-artifacts.zip` preserves all ten raw audio/model files with hashes in `archive.json`.

This was concurrent independent component work, not a causal voice conversation or sustained load test. Requests repeated in ascending 1/2/4 order on a warm server; speech inference retained its first cold call, and production STT prewarm was not invoked. Cache/order effects prevent a scaling or throughput claim. Launch flags selected decode/prompt concurrency 1, prefill steps of 256, one prompt-cache entry and a nominal 256 MiB cache budget; exact arguments, weights and package hashes are in `local-llm-server/launch.json`. A subsequent installed-source audit found that MLX LM 0.31.3 serves PRISM's seeded requests sequentially and bypasses that byte-budget flag; logs already showed 0.34 GB retained cache. The flag was not an enforced memory bound (`results/iteration3/cache-source-audit.json`). Listener PID checks tie the observed local requests to the task-owned process, without independent process attestation. It was stopped after experiments to release resources.

Before running, review found a probe-scoring bug that accepted malformed route arguments. Tightening keys/types changed the same 18 scorer tests from 8/18 to 18/18 before any capacity inference. No measured outputs were discarded or retrospectively rescored.

With the pinned server started using the recorded launch arguments, reproduce into new paths:

```bash
.venv/bin/python scripts/local_navigation_experiment.py --out results/navigation/new-run --case-timeout 240
.venv/bin/python -m tests.synthetic.run_synthetic --profile local-mac -j 1 -v --out results/synthetic-new.json
unzip -n results/iteration2/local-speech-run1/generated-audio.zip -d results/iteration2/local-speech-run1
.venv/bin/python scripts/local_overlap_experiment.py --out results/overlap/new-run \
  --audio results/iteration2/local-speech-run1/raw/direct-stt-16k.wav \
  --llm-pid YOUR_SERVER_PID --batch-timeout 240
```

The remaining priorities are:

1. Constrain named-place selection and unsupported optional filters, and detect omitted requested actions. Preserve these model failures as development regressions; do not describe subsequent tuning as held-out evaluation.
2. Reduce planner/result latency with controlled model/cache comparisons while retaining effect and response checks.
3. Supply LiveKit credentials and benchmark audio, then measure live recognition during playback, echo, false interruptions and audible stop latency with the commands in the README. Current text/component timings cannot substitute for those measurements.
4. Validate CUDA/MLX evaluation-ASR comparability, confirm the organizer-issued Theme 05 brief, and add durable backend reconciliation and bounded telemetry archival before stronger production guarantees.
