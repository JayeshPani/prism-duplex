# PRISM Duplex

An interruptible voice-agent prototype with local speech recognition, planning and speech synthesis, a LiveKit audio connection, and a simulated Bengaluru navigation demo. The project identifies itself with Samsung PRISM Theme 05; an organizer-issued brief has not yet been independently verified.

**Current evidence:** 797 Python unit tests, six frontend tests and the production build pass. The latest development checkpoint fulfills **4/6 candidate voice cases**: hospital and airport distance queries, querying hospital distance while airport navigation stays active, and explicit hospital navigation. The office-time question asks for a retry; one ambiguity case times out before input publication. A launcher error leaves only one completed baseline conversation, so no six-pair comparison is claimed. That matched hospital-distance case improves from a lookup-only reply to the returned 2.2km answer. **116 localhost RTC capture attempts** remain archived, plus the latest separate parser failure. Raw trace equality is 7/7 for the latest captured rooms; trusted equality is 2/7. This is a tested development prototype, not production-readiness evidence. Start with [the release checkpoint](docs/iteration20.md), [the clarification comparison](docs/iteration19.md), [the speech-pause comparison](docs/iteration18.md), [remaining requirements](docs/completion-audit.md) and [research/benchmark audit](docs/research.md).

## What runs where

| Component | Implementation | Boundary |
|---|---|---|
| Recognition | Parakeet-TDT 0.6B v2, MLX or NeMo | Local, completed VAD segments; no streaming partial ASR |
| Planner/responder | Qwen3 through MLX LM or vLLM; direct summaries for recognized navigation commands | Local for benchmark runs; optional hosted profile is development-only |
| Voice activity and interruption | Silero + explicit LiveKit VAD mode | Local; cloud turn/interruption defaults explicitly disabled |
| Speech | Kokoro-82M via ONNX | Local, whole-utterance synthesis |
| Audio transport | LiveKit | Self-hosted loopback tested with synthetic speech; Cloud sends audio off-device |
| Evaluation judge | Exact fallback by default; optional GPT-4o | Hosted judging is separate from agent inference |
| Navigation | Deterministic coordinates and simulated ETA | No traffic, GPS, real routing, or vehicle control |

Microphone → LiveKit → local VAD/segment STT → optional whole-span re-transcription → intent resolver → commit hold → validated dependency graph → tools → grounded response → local TTS → LiveKit. Each room owns its intent, ledger and navigation state. STT and TTS engines are shared and serialized, so room-state isolation does not imply latency isolation.

## Correctness contract

- A new finalized transcript invalidates the older intent immediately. Pending calls and reads are cancelled; intent is checked again just before dispatch and before speaking results.
- **A write already dispatched can still complete.** Later writes are serialized behind it. Cancellation does not undo external effects. “No stale call ever executes” is not a supported blanket guarantee.
- An intentional new turn gets a new operation identity. An explicit retry of that operation uses the same identity. Successful effects are suppressed within the operation, without blocking a later legitimate identical request. Retry call IDs are bound to their original arguments.
- Backend errors are failures, not completed actions. Exceptions/timeouts/cancellation after a write starts are **unknown** until reconciled; identical uncertain actions are not automatically retried.
- The ledger is in memory. There is **no exactly-once guarantee across process crashes, restarts, lost responses or external backends**. Durable operation IDs and backend idempotency are required for that claim.
- Malformed resolver plans, duplicate call IDs, missing dependencies, cycles, unknown tools, missing required arguments and invalid typed literals are rejected before execution. A malformed sibling invalidates the whole plan. Result references must match exact paths and their resolved values are checked before dependent dispatch; independent actions already completed are not rolled back.
- A narrow navigation contract checks complete, nonempty plans for recognized whole commands to a grounded destination. It requires a current compute→start dependency and exactly one navigation mutation. A violation permits one repair with the original request/context before any dispatch; a second failure asks for clarification. After a matching structured ambiguity, a whole stored name, ID or alias can carry the original navigation request forward and must satisfy that same contract, including complete/nonempty planning. The latest answer remains verbatim in the trace. Cancellation, questions and new commands consume the pending context without becoming a continuation. Unknown, qualified, compound and model-only clarification requests remain ordinary planning; this is not general task-completeness validation.
- Reads are cached only when a tool declares its context. Nearby searches include navigation version; place matching accepts exact IDs/names/aliases and unique whole-word partial names. Unknown or ambiguous places ask for clarification without changing the route. Waypoints retain canonical IDs.
- Starting the active route ID again preserves its stops. A fresh route computation after navigation changes gets a new route ID and replaces the previous stops when activated. Adding an existing stop is a no-op. Airport → office → airport is a valid sequence. Cancelled navigation clears the map.
- Recognized action-only navigation commands can receive direct summaries of successful returned facts, including replaced destinations, no-op status, stops and current arrival estimates. A redundant successful lookup of the same confirmed destination no longer forces model synthesis; exact destination IDs and a single returned place establish that match. Extra questions, unfamiliar phrasing, other independent reads and unsupported outcomes retain model synthesis. The guard uses the complete utterance supplied to the resolver; fallback receives that request and any repaired interpretation separately. This preserves information at the handoff, without guaranteeing ASR, planning or model-answer fidelity.
- Live telemetry persists a dispatch intention off the event loop before backend entry, checks intent again after that wait, then updates the same row when observation ends. Failed/cancelled attempts remain visible; a crash before backend entry can leave an intention without an actual effect. Start-log failure prevents dispatch. Pending disk work is owned and reported during shutdown; it cannot relabel a known backend success as unknown. Blocked retries and reused reads remain separate trace events.

An unfinished resolution asks the user to finish rather than dispatching a guess after a timer. VAD starts the SDK pause. A public audio-output guard now defers its resume while local recognition remains pending; empty recognition permits resumption, while nonempty/error/cancel/timeout cancels the captured reply and waits for playback cleanup. A thirty-second recognition-wait deadline requests interruption; release still awaits generation cleanup. Empty-result recovery passes controlled SDK checks, but the eleven played acoustic non-speech controls never triggered VAD and therefore did not exercise recovery. Finalized input interrupts obsolete speech handles. The short voiced probes also produced nonempty transcripts that replaced the original result. Local correction captures measure received PCM cessation separately from handle cancellation; physical audible-stop latency and echo behavior still need measurement.

Owned planning and speech tasks have bounded shutdown and report failures. Closing the voice session also ends its room job, which owns coordinator, HTTP and trace cleanup. Iteration19's ten rooms completed application cleanup and both workers exited normally, adding to iteration18's 21 rooms and the forty-four rooms in the preceding ten cleanup suites. Native work and SDK teardown can still delay shutdown. Trace file operations run on a per-room worker; cleanup checks accepted events were flushed, and reports failures or rejected events. Pending history is unbounded, and flush is not power-loss durability. Tool-plan narration stays in the trace; acknowledgments, clarifications and result summaries are spoken. A failed spoken summary is reported separately from a successful tool action; it cannot convert backend success into failure or speak after a newer intent supersedes it.

## Reproduce development checks

Python 3.11 and Node 22.18+ support the core checks; no credentials or weights are required. The recorded 797-test run also had the pinned SDK/audio packages installed. A minimal `requirements/test.txt` environment skips SDK-dependent component tests; the full local environment is specified in `requirements/mac.txt`.

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements/test.txt
git submodule update --init --recursive
.venv/bin/python -m pytest -q
.venv/bin/python scripts/reliability_experiment.py --out results/reliability/new-run --repeats 5
.venv/bin/python scripts/probe_fdb_contract.py bench/Full-Duplex-Bench/v3 --out results/reliability/new-contract-probes.json
cd web
npm ci
node --experimental-strip-types --test lib/events.test.ts
npm run build
```

The experiment declares seven independent scenarios before running, exercises two scaled commit policies and saves all coordinator events and backend attempts. It uses scripted planning and simulated tools. It is **development evidence, not a held-out benchmark or acoustic latency measurement**.

## Inspect the navigation demo without credentials

```bash
cd web
npm run dev
```

Open the browser, select **Review a saved trace**, and import `results/reliability/final/trace-fixed-return_to_destination-1.jsonl`. It shows airport → office → airport and the final route. Import `results/reliability/map-cancel.jsonl` to inspect cancellation. The timeline distinguishes waiting, running, succeeded, failed, cancelled, reused and unknown outcomes, with separate execution IDs for overlapping calls. Imported traces are explicitly labeled; they do not play audio. Review expects one room per file. Map tiles require internet access.

## Run the live voice demo

Requires a LiveKit server and credentials (Cloud or self-hosted), local model weights, audio dependencies and microphone access. Self-hosted loopback transport has been tested with prerecorded synthetic speech; see [the exact local launch and capture commands](docs/iteration4.md). Physical microphone interaction remains untested. The tested M2 Pro has 16 GB and showed substantial existing swap use during the audio trials. A small 1/2/4-room component probe completed with all three local models loaded, but it is not a live-room capacity guarantee. A separate cache comparison reduced repeated substantive-text median latency from about 17.8 to 9.1 seconds while retaining all declared effects; first fresh-process responses still took about 17.7 seconds. This setup still needs latency work.

```bash
cp .env.example .env.local  # fill in your LiveKit settings
uv pip install --python .venv/bin/python -r requirements/mac.txt
source .venv/bin/activate
bash scripts/download_models.sh local-mac
PRISM_MODEL_PATH="$(python -m agent.model_assets llm --profile local-mac --local-files-only)"
python -m mlx_lm server --model "$PRISM_MODEL_PATH" --port 8081 --prompt-cache-size 2 \
  --chat-template-args '{"enable_thinking": false}'
# In another terminal with the venv active:
python -m agent.main dev
# Give web/.env.local the same LiveKit settings, then:
cd web && npm ci && npm run dev
```

Browser `car-*` rooms use navigation; `web-*` rooms use Assistant mode. Other rooms use benchmark tools. Result speech defaults to enabled in all modes. `MODE=car|assistant|bench` explicitly overrides room selection; `PRISM_BENCH_SPEAK_RESULTS=0` is a recorded benchmark-only silence ablation. Try a destination correction, then a coffee stop, then cancellation and a return to the first destination. Airport navigation, destination correction and waypoint addition have recorded localhost trials, including successful iteration13 examples. Each iteration17 arm clears both cancelled routes and completes both airport→office→airport cases with the airport, replaced-office and 88-minute facts in the received-speech review. Two plans across the three arms use a logged repair before dispatch. The one-entry arm lets three old replies finish instead of interrupting them; final backend and reply correctness do not erase those failures. Four initial packets without sender identity across the arms remain excluded from trusted traces. Iteration14's missing activation and both failed replies, iteration15's omitted replacement fact, and all prior capture failures remain unchanged.

## Benchmark reproduction and reporting

```bash
PRISM_PROFILE=local-mac JUDGE=none LIMIT=10 bash scripts/run_benchmark.sh
# Frozen full run, after validating development configuration:
PRISM_PROFILE=local-mac JUDGE=none bash scripts/run_benchmark.sh
# CUDA host with official evaluation ASR and optional hosted judge:
PRISM_PROFILE=local-cuda JUDGE=openai bash scripts/run_benchmark.sh
```

The runner rejects hosted agent profiles/non-loopback LLM endpoints, copies inputs into a distinct run directory, preserves dispatch logs, captures environment/code/config provenance and reports missing/excluded outputs. The benchmark submodule is pinned at `3e799c45a045256f47d5f1c9cda90157e2d2ec9e`. Qwen and Parakeet downloads and loaders use the exact Hub revisions in `agent/config.yaml`; the LLM server receives that snapshot's local directory. MLX clients use `default_model` to address the server's loaded snapshot. Model overrides require a matching `PRISM_LLM_REVISION`; `PRISM_ALLOW_UNFROZEN_MODELS=1` is development-only and rejected by benchmark preflight.

Snapshot selection, successful loader return, and observed inference are distinct evidence. The manifests do not claim that selecting a snapshot proves a server loaded it. Kokoro files are checked against the reference hashes observed in our successful speech run; these freeze the bytes used by PRISM but are not publisher-authenticated digests. See [model provenance limits](docs/iteration2.md).

Mac runs substitute MLX evaluation ASR for NeMo/CUDA. Label them **adapted FDB-v3** until decoding/timestamp equivalence is measured. Tool pass-rate verifies a tool-name multiset and expected arguments; it does not establish successful backend effects, correct spoken answers or dynamic-reference provenance. `JUDGE=none` is a fallback, not uniformly stricter judging. Do not compare it as equivalent to hosted semantic scoring.

Historical reports contain 5/10 and 6/10 passes, both with `JUDGE=none`; they are not new results for this revision. The saved 24/24 synthetic reports rescore to 17/24 and 18/24 under the new conservative development scorer. Some differences are formatting acceptance, so these are scorer-sensitivity results, not measured model degradation. See raw results under `results/reliability/`.

The earlier local Qwen development run scored 17/24 (`results/iteration2/local-synthetic-run1/`). Its four navigation cases improved from 2/4 to 4/4, although a generic category search happened to select the requested café. In iteration 3, named-place guidance resolves that café by identity, and all four navigation cases still pass. The broader development run remained 17/24 with a new premature apartment search; correcting required tool metadata produced 19/24 in the final run. All runs and failures remain available. These results do not establish general planning accuracy or held-out benchmark performance. The later twelve audio probes preserved all twelve airport effects: nine non-speech controls retained the complete result, while three voiced fragments stopped it. The same run exposed a 613 ms event-loop stall during synchronous telemetry I/O and required forced worker termination after a twenty-second grace period. Iteration 6 moves attempt logging off the loop and tests cancellation/cache boundaries. Iteration 7 adds the missing session-close-to-job-shutdown connection and repeats eight RTC captures with the current logger: 30 successful attempts, ten expected effects, all agents departed and worker exit 0 without escalation. Full event equality holds for 3/8; five miss only initial listening. Whole-stream ASR retains the expected core facts with proper-name differences. Three watchdog stalls without sampled stacks and other SDK warnings remain visible; these runs do not establish a latency improvement or general bounded process exit.

No new held-out benchmark data informed this implementation. Existing benchmark exposure means future repeated runs should be described as regression evaluation. The [cache comparison](results/iteration17/comparison-report.json) retains all twelve cases and thirty input turns. In two-entry → one-entry → two-entry order, final-input-to-reply-finish p50/p95 is **9.689/22.281, 21.385/25.166 and 11.765/22.967 seconds**, each measured for 4/4 cases meeting backend and final-speech criteria. These timings do not require successful interruption or full trusted-trace equality. An interruption-requested handle does not establish prompt cutoff: retained old PCM continues for 4.917 seconds after input onset in one final two-entry case and 7.875 seconds in one one-entry case. These are received-audio observations, not physical speaker measurements. Logged logical cache peaks are **0.75/0.38/0.75 GB**; they are not total memory measurements. The one-entry arm is not adopted. Fixed order, shared hardware, adaptive prompts and differing segmented PCM prevent causal claims.

The iteration17 arms retain 116 warning rows, twelve server `JS_FAILED` labels and one real LLM-server `BrokenPipeError`. All 24 recorded suite/reviewer PIDs were absent after completion; frozen sources, inputs and ten shared model references matched. The [identity-boundary investigation](results/iteration17/identity-boundary-investigation.json) confirms that blank native sender identity must remain untrusted; the exposed receiver fields support no safe promotion fix, and its upstream cause remains unproven. [Iteration18](docs/iteration18.md) supports the recognition-aware output guard; [iteration19](docs/iteration19.md) carries the hospital navigation request through clarification with deliberately narrow recognition/grounding requirements. [Iteration20](docs/iteration20.md) requires a matching route read for supported distance/driving-time questions and prevents those questions from mutating active navigation. Remaining priorities include unsupported planner place forms such as `the office`, recognition of place names, startup identity, simultaneous live-room capacity, controlled policy comparisons and a fresh adapted FDB run. Physical playback, human overlap and microphone echo remain unverified. Authorized organizer-folder access was denied by an administrator-policy check, so Theme 05 compliance is unverified. The distinct official FDB dataset URL remains untried and policy scope unresolved; access is neither confirmed permitted nor confirmed blocked.
