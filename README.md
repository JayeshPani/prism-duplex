# PRISM Duplex: an interruptible real-time voice agent

Samsung PRISM, Theme 05. A LiveKit voice agent that keeps talking while it
works, lets you change your mind mid-sentence, and never performs the same
state-changing action twice. It is evaluated on **Full-Duplex-Bench v3**
(FDB-v3), and it includes an **in-car navigation extension**.

**Model declaration.** The whole stack runs locally; no hosted model API is
called at evaluation time.

| Part | Model | Runtime |
|---|---|---|
| LLM (planner + responder) | Qwen3 (4-bit), thinking disabled, temperature 0, seed 7 | `mlx_lm.server` on Apple Silicon / vLLM on NVIDIA |
| Speech-to-text | NVIDIA Parakeet-TDT 0.6B v2 | parakeet-mlx / NeMo |
| Text-to-speech | Kokoro-82M v1.0 | kokoro-onnx |
| VAD | Silero | livekit-plugins-silero |
| Transport | LiveKit Cloud (free tier) | livekit-agents 1.8.3 |

The only key needed is a free LiveKit Cloud project. An OpenAI key is needed
only if you want the official GPT-4o judge when scoring (`JUDGE=openai`).

---

## Architecture

```
            benchmark WAV / microphone
                       │  LiveKit room (audio)
                       ▼
┌─────────────── FAST PATH ───────────────┐
│ Silero VAD ─▶ Parakeet STT (~0.2 s)      │
│ end of utterance ─▶ ack timer (700 ms):  │──▶ "One moment."  (no dead air, never "done")
└──────────────────┬──────────────────────┘
                   │ transcript segment
                   ▼
┌─────────────── COORDINATOR ──────────────────────────────────────────┐
│ merge with still-uncommitted segments (the user only paused)          │
│ IntentResolver (LLM, JSON): repair disfluencies, last value wins,     │
│   slots, corrections, and a plan of tool calls with $c1.x references  │
│ CommitGate: hold 350 ms (reads) / 700 ms (writes) / 1.8 s (cut off)   │
│   user speaks again ─▶ pause; new words ─▶ discard plan, merge, replan│
│ SlotStore: session-scoped slots with correction history               │
│ Ledger: state-changing calls keyed by (tool, canonical args);         │
│   an identical action is blocked, never re-executed                   │
└──────────────────┬───────────────────────────────────────────────────┘
                   ▼ committed plan
┌─────────────── SLOW PATH ───────────────┐
│ Executor: asyncio DAG, chains resolve    │──▶ tools (FDB mocks / car backend)
│   $c1.flights[0].flight_id; stale reads  │
│   cancelled on correction; started writes│
│   protected and finished exactly once    │
│ Responder: grounded result sentence      │──▶ Kokoro TTS ─▶ room
└──────────────────────────────────────────┘
   every decision ─▶ event bus ─▶ JSONL trace + data channel ─▶ web UI timeline
```

**Our core design choice: no stale call ever executes.** FDB-v3 passes a
scenario only if the exact multiset of expected tools is called with correct
arguments, so one stale `search_flights(Paris)` before "…actually Berlin"
fails the whole scenario. We do not hide such calls from the log. Every
executed call is logged exactly as the reference agents log it. Instead,
nothing is executed until the utterance has been resolved and has passed the
commit gate.

Code map:

| Path | What |
|---|---|
| `agent/main.py` | LiveKit worker; one coordinator per room; no shared state across rooms |
| `agent/coordinator/` | resolver, commit gate, executor, ledger, responder, events |
| `agent/tools/bench_tools.py` | FDB-v3 tool schemas; implementations are the benchmark's own `mock_apis.py` |
| `agent/tools/car_tools.py` | **Extension**: in-car navigation backend |
| `agent/pipeline/` | local STT/TTS as LiveKit plugins |
| `web/` | Next.js demo UI (transcript, time ribbon, state, map) |
| `scripts/run_benchmark.sh` | the one-command reproduction |
| `tests/unit`, `tests/synthetic` | coordinator tests; our own disfluent dev set |

---

## Run the benchmark (one command)

```bash
cp .env.example .env.local        # add your LiveKit URL / key / secret
bash scripts/run_benchmark.sh
```

The script does the following:
1. Checks your tools and keys.
2. Creates a Python 3.11 venv from the pinned `requirements/`.
3. Downloads the pinned models and the FDB-v3 audio.
4. Starts the local LLM server and the agent worker.
5. Runs the benchmark's own `run_tool_benchmark_all_released.py` and evaluators.
6. Writes everything to `results/run_<timestamp>/`: reports, raw logs, traces, config and `SUMMARY.md`.

It picks a profile by itself: Apple Silicon uses `local-mac`, an NVIDIA GPU
uses `local-cuda`. Options:

| Variable | Meaning |
|---|---|
| `PRISM_PROFILE=local-cuda` | force a profile (see `agent/config.yaml`) |
| `JUDGE=openai` | official GPT-4o judge (needs `OPENAI_API_KEY`); default `none` is exact-match |
| `LIMIT=10` | quick run on the first 10 examples |

Requirements: `git`, `ffmpeg`, `curl`; macOS 14+ on Apple Silicon (24 GB), or
Linux with an NVIDIA GPU (CUDA 12.x).

On a machine without CUDA the benchmark's NeMo ASR (which calls `.cuda()`)
is replaced, in `scripts/fdb_infer.py`, by the same Parakeet checkpoint on
MLX. On the organizers' CUDA machine the benchmark scripts run unmodified.

---

## Extension: in-car navigation

A hands-free driving assistant built on the same coordinator; only the tool
manifest changes (`agent/tools/car_tools.py`). It uses a deterministic
Bengaluru map with six tools:

| Tool | Kind |
|---|---|
| `compute_route` | read, slow (~2.5 s) |
| `start_navigation` | state-changing |
| `add_waypoint` | state-changing |
| `find_nearby` | read |
| `cancel_navigation` | state-changing |
| `search_destination` | read |

What it demonstrates:
- **Destination change mid-sentence.** "Take me to the airport… actually, the
  office first" computes only the office route.
- **Destination change after commit.** The in-flight airport route computation
  is cancelled as stale, and navigation starts exactly once.
- **Barge-in.** Interrupt the spoken ETA with "add a coffee stop": the speech
  stops, one waypoint is added, and the ETA is updated.
- **Duplicates are blocked.** Repeating a request, e.g. "add that coffee stop",
  does not add it twice; the ledger blocks it.

Run it live:

```bash
source .venv/bin/activate
python -m mlx_lm server --model mlx-community/Qwen3-8B-4bit --port 8081 &
python -m agent.main dev          # worker (rooms named car-* run the extension)
cd web && npm install && npm run dev   # http://localhost:3000, choose "In-car"
```

---

## Honesty notes

- **No benchmark answers are used.** No FDB-v3 transcripts, answers or item
  entities appear in prompts, code or tuning data. The resolver's few-shot
  examples are our own and use tools that do not exist in the benchmark. We
  tuned on `tests/synthetic/scenarios.yaml`, which we wrote ourselves, and ran
  FDB-v3 only to measure.
- **Each conversation starts fresh.** Nothing is cached across scenarios:
  every LiveKit room gets a new coordinator, ledger, slot store and tool state.
- **Judge.** Our self-reported numbers state which judge produced them. Only
  the organizers' re-run with the pinned judge is official.
- **Filler.** The "One moment." ack is a generic phrase, spoken only when the
  planner is slower than 700 ms. It never claims completion.

## Results

See `results/` (filled in after the first full run).
