# Local transport audio and correction trials

This iteration connects the existing agent to self-hosted LiveKit on the same M2 Pro used in [iteration 3](iteration3.md). It sends frozen synthetic user speech through an actual RTC track and records the agent's received PCM, coordinator events and every tool attempt. Qwen, Parakeet and Kokoro inference remain local. No hosted judge or Cloud credentials participate.

## Protocol and scope

The initial protocol was written before generating the input assets. The baseline says “Navigate to the airport.” and expects one airport navigation effect followed by a grounded spoken result. The correction case sends “Actually, go to the office instead.” during observed substantive result audio; it expects the already-completed airport action followed by one office action and a new office result. The earlier airport action is not classified as stale because it completed before the correction.

Both pilot captures completed. A separate repeat protocol then declared four additional trials per case in alternating order, using unchanged input assets, agent source and configuration. All ten observations, including the first fresh-server model request, are retained. These are five repetitions of each of two development cases, not ten independent examples, a held-out benchmark, a controlled acoustic performance comparison or concurrent-room evidence.

The input voice is Kokoro `af_heart` at speed 1.1, also used for agent output, on separate RTC tracks with no physical echo path. The two input WAVs last 1.429 and 1.941 seconds. This does not test human speech, microphones, loudspeakers, echo cancellation, road noise, false barge-ins, natural pauses, WAN transport or actual vehicle control. The map and route times remain simulated.

The driver waits for the agent's `session_started` event and input-track subscription before publishing paced 20 ms PCM frames, followed by up to one second of explicit silence. Its output receiver is registered before connection; it does not wait for a lazily published output track before sending input. A correction requires an active result speech handle, observed speaking state, fresh non-silent PCM and a quiet boundary after any earlier acknowledgment. It rechecks these conditions immediately before publishing the correction. It retains every received frame and packet and waits for the current result handle to finish plus a quiet tail, with explicit deadlines.

Speech handles and text events do not carry a shared RTP segment identifier. Their association therefore relies on event order and quiet boundaries. Frame arrival and data events cross separate transport channels; this is explicitly a heuristic, not proof of exact sentence-to-packet attribution. A completed capture alone is not an accuracy or intelligibility pass.

## Observed outcomes

All ten captures produced their declared navigation effect sequence: five airport-only cases and five airport-then-office cases. All **45 backend attempts completed successfully**, producing **15 navigation effects**; all 15 recognized user turns match the two declared phrases. No additional navigation effect or stale tool result appears in these trials. These narrow checks do not establish general task accuracy.

Complete client/server coordinator-event equality holds for **5/10 captures**. The other five omit only the initial server `agent_state=listening` event before `session_started`; their captured events exactly match the remaining server suffix. Every tool, transcript and speech event is present. The strict complete-capture check remains failed for those five runs, rather than being silently relaxed into a perfect aggregate score.

After the timed captures and server shutdown, a separate frozen protocol transcribed all ten complete received PCM streams through the pinned production Parakeet model, without trimming, retries or silence removal. All five baseline transcripts contain airport navigation and the 88-minute ETA. All five correction transcripts contain the interrupted “Navigation to” fragment, then office navigation and replacement of the airport destination. The ASR consistently renders “Kempegowda” as “Kempegauda” and “Manyata” as “Maniata”. Fifteen phonemizer warnings occurred during result synthesis. The recognized facts survived, but these observations cannot distinguish ASR spelling from pronunciation errors or explain the warnings. No human listening or independent speech-quality score is claimed.

These are descriptive p50/p95 values in seconds, using nearest rank over five observations per column; p95 is therefore the maximum. The cold pilot is retained. Each column starts from that user utterance's scheduled threshold speech end:

| Observed boundary | Airport-only case | Initial airport turn in correction case | Office correction turn |
| --- | ---: | ---: | ---: |
| Finalized transcript received | 1.654 / 1.675 | 1.654 / 1.681 | 1.650 / 1.733 |
| First acknowledgment PCM | 2.939 / 3.246 | 2.926 / 2.960 | 3.060 / 3.340 |
| First tool-start notification | 5.197 / 14.574 | 5.018 / 5.073 | 7.376 / 7.561 |
| Navigation success notification | 7.704 / 17.077 | 7.525 / 7.579 | 9.883 / 10.067 |
| First substantive result PCM | 11.719 / 21.684 | 9.397 / 9.446 | 13.900 / 14.060 |
| Result speech handle finished | 18.006 / 27.963 | Interrupted; not finished | 21.788 / 21.957 |

The later correction response is still slow despite prompt acknowledgment. These serial, repeated-input observations include cache and fixed-order effects; differences between columns are not a controlled speedup comparison. Tool-start notifications mark dispatch intention before backend entry, and received success notifications add data transport delay to the simulated effect.

Across the five interruptions, the last old-result-correlated non-silent frame arrived **0.740 / 0.760 seconds** after correction speech onset (p50/p95), followed by a below-threshold frame at **0.760 / 0.780 seconds**. User-speaking events arrived at **0.210 / 0.250 seconds**, agent-listening events at **0.660 / 0.697 seconds**, and coordinator interruption requests at **3.428 / 3.512 seconds**. The earlier cessation is consistent with SDK speech interruption, but these captures do not isolate its internal cause or physical playback timing.

## Timing definitions

All input/received-event timings use the recorder's monotonic clock. Input speech onset/end are the scheduled edges of the first/last 20 ms WAV window above −40 dBFS; they are not physical acoustic measurements. Received speech activity uses every PCM frame above the same threshold. Input queue drainage and frame arrival jitter are retained separately.

A result's recorded `last_loud_at` can undercount its tail after the agent state changes to listening, because the recorder then stops assigning a speech handle to frames. Interruption review must scan **all** received non-silent PCM before the next acknowledgment. The later coordinator `interruption_requested` event follows finalized recognition; it does not establish the time that VAD paused outgoing speech or the time sound stopped at a speaker. No physical audible-stop result is claimed.

## Environment and memory

The experiment used macOS 15.3.1 on a 10-core M2 Pro with 16 GiB RAM, Python 3.11.13, LiveKit server 1.13.7, LiveKit Agents 1.8.3 and RTC 1.1.18. The pinned Qwen3-8B-4bit snapshot, Parakeet revision and Kokoro hashes match the earlier local experiments. MLX LM 0.31.3 used two cache entries after the separate A/B/A experiment, with seed 7. Its seeded path is sequential and does **not** enforce the recorded cache-byte flag.

Listener inspection confirmed signaling on TCP `127.0.0.1:7880`, media on UDP `127.0.0.1:7882`, model serving on `127.0.0.1:8081` and worker health on `127.0.0.1:8082`. The worker uses thread jobs and shared speech models. Local transport does not establish an air-gapped run: the default SDK may contact public ICE/STUN discovery services. Public `devkey`/`secret` credentials belong only to this loopback development configuration.

During the eight repeats, 454 samples over 229.5 seconds recorded a minimum 1.199 GB system available memory. Swap was already 6.615 GB at the start and ended at 7.326 GB, with a sampled maximum of 7.563 GB. Recorded RSS peaks were 4.739 GB for the model server, 0.672 GB for the worker and 0.058 GB for LiveKit. These decimal GB figures describe sampled residency, not complete model allocations or a safe total-memory bound. Samples started after loading, omitted a descendant census and cannot isolate other application activity. RSS and unified-memory measures must not be added as independent allocations. The evidence does not support a sustained-capacity claim.

After capture completion, the three owned process commands were checked against launch receipts and terminated. Supervisor receipts record worker/LiveKit exit 0 and model-server termination by SIGTERM. All raw outputs remain available; received-speech review runs separately after timing collection.

## Reproduce a capture

Use the dependencies and pinned model preparation in the README. The saved input WAVs are reusable development assets. Start each long-running component in a separate terminal, with the virtual environment active. The checked-in local server configuration binds the public development credentials to loopback:

```bash
livekit-server --config results/iteration4/local-stack-run1/livekit.yaml
```

Start the model server with the measured two-entry configuration:

```bash
PRISM_MODEL_PATH="$(python -m agent.model_assets llm --profile local-mac --local-files-only)"
python -m mlx_lm server --model "$PRISM_MODEL_PATH" --host 127.0.0.1 --port 8081 \
  --chat-template-args '{"enable_thinking":false}' \
  --decode-concurrency 1 --prompt-concurrency 1 --prefill-step-size 256 \
  --prompt-cache-size 2 --prompt-cache-bytes 268435456 --max-tokens 500
```

Use a new directory for worker evidence. The byte flag above reproduces the command, not an enforced cap on this MLX version:

```bash
export LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=secret
export PRISM_PROFILE=local-mac HF_HUB_OFFLINE=1
export PRISM_TRACE_DIR=results/iteration4/new-live-run/traces
export FDB_TOOL_LOG=results/iteration4/new-live-run/tool-calls.jsonl
export FDB_HEARTBEAT_LOG=results/iteration4/new-live-run/heartbeat.log
export PRISM_MODEL_RECEIPTS_DIR=results/iteration4/new-live-run/model-receipts
python scripts/local_livekit_worker.py \
  --manifest results/iteration4/new-live-run/worker-manifest.json
```

With the same LiveKit environment in the recorder terminal, run the two captures sequentially. The driver refuses an existing output directory:

```bash
python scripts/local_livekit_audio_experiment.py \
  --input results/iteration4/audio-assets/navigate.wav \
  --out results/iteration4/new-live-run/baseline \
  --worker-manifest results/iteration4/new-live-run/worker-manifest.json \
  --protocol results/iteration4/protocol.json --ready-timeout 45 --response-timeout 90
python scripts/local_livekit_audio_experiment.py \
  --input results/iteration4/audio-assets/navigate.wav \
  --correction results/iteration4/audio-assets/correct.wav \
  --out results/iteration4/new-live-run/correction \
  --worker-manifest results/iteration4/new-live-run/worker-manifest.json \
  --protocol results/iteration4/protocol.json --ready-timeout 45 --response-timeout 90
```

## Evidence and next checks

`results/iteration4/protocol.json` and `repeat-protocol.json` preserve declarations; `audio-assets/report.json` records input generation and hashes. Each `rtc-*` directory contains its launch/configuration report, sent-input schedule, raw data, frame arrival log and received PCM. Shared traces, all tool attempts, server logs, model receipts, launch commands and cleanup records are under `local-stack-run1/`. `repeat-memory.jsonl` and its summary preserve the memory observations. Driver regressions bring the suite to **480 passing Python tests** in `rtc-driver-unit-tests-final.txt`.

`aggregate-review.json`, per-run `transport-review.json` and `audit_transport.py` preserve the derived checks, every observation and timing formulas. `received-speech-review/{protocol,report,qualitative-review}.json` preserve the later whole-stream recognition review. Exact concatenated WAVs retain all received samples and silence but insert no missing callback-arrival gaps; use the frame log for transport timing. They can be played for content review, not as reconstructed playout timelines. The real correction's server trace can also be imported into the existing web review UI:

```text
results/iteration4/local-stack-run1/traces/car-audio-8b7085723e4b4ffa9b18c11bf623b16c.jsonl
```

To repeat the derived transport audit or offline received-speech recognition:

```bash
python results/iteration4/audit_transport.py
python scripts/review_received_speech.py --input-root results/iteration4 \
  --out results/iteration4/new-received-speech-review
```

The next evidence should cover non-speech/false interruptions, continued speech after pauses, microphone echo and noisy human input, corrections while tools are slow, and actual simultaneous rooms. These require separate expected outcomes and frozen protocols. Further response-latency work must retain task completion and meaningful result speech; acknowledgment speed alone is insufficient. A fresh adapted FDB audio run and organizer-issued Theme 05 verification also remain outstanding.
