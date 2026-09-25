#!/usr/bin/env bash
# One command: install -> start local LLM server -> start agent -> run FDB-v3 -> evaluate.
#
#   bash scripts/run_benchmark.sh                 # auto profile (Apple Silicon -> local-mac, NVIDIA -> local-cuda)
#   PRISM_PROFILE=local-cuda bash scripts/run_benchmark.sh
#   JUDGE=openai bash scripts/run_benchmark.sh    # official GPT-4o judge (needs OPENAI_API_KEY)
#   LIMIT=10 bash scripts/run_benchmark.sh        # quick run on the first 10 examples
#
# Needs: python3.11+, ffmpeg, git, and .env.local with LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
PROVIDER="${PROVIDER:-prism}"
JUDGE="${JUDGE:-none}"
DATA_DIR="$ROOT/bench/data/fdb_v3_data_released"
OUT="$ROOT/results/run_$STAMP"
DATA_GDRIVE_ID="1SO_4MTazWQ_jvCx0dtmpQ-t40bdd07yz"   # from the FDB-v3 README
mkdir -p "$OUT"
log() { printf '\n\033[1m[prism] %s\033[0m\n' "$*"; }
die() { printf '\n[prism] ERROR: %s\n' "$*" >&2; exit 1; }

# ── 0. checks ────────────────────────────────────────────────────────────────
for c in git ffmpeg curl; do command -v "$c" >/dev/null || die "'$c' is required (macOS: brew install $c, Ubuntu: apt install $c)"; done
[ -f .env.local ] || die ".env.local missing. Copy .env.example to .env.local and fill in the LiveKit keys."
for k in LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET; do
  grep -qE "^$k=.+" .env.local || die "$k is not set in .env.local"
done
if [ -z "${PRISM_PROFILE:-}" ]; then
  if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then PRISM_PROFILE=local-mac
  elif command -v nvidia-smi >/dev/null; then PRISM_PROFILE=local-cuda
  else die "No Apple Silicon or NVIDIA GPU found; set PRISM_PROFILE explicitly."; fi
fi
export PRISM_PROFILE
log "profile=$PRISM_PROFILE judge=$JUDGE provider=$PROVIDER output=$OUT"

# ── 1. python env (pinned) ───────────────────────────────────────────────────
command -v uv >/dev/null || { log "installing uv"; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
[ -d .venv ] || uv venv -q -p 3.11 .venv
# shellcheck disable=SC1091
source .venv/bin/activate
REQ=requirements/mac.txt; [ "$PRISM_PROFILE" = "local-cuda" ] && REQ=requirements/cuda.txt
log "installing $REQ"
uv pip install -q -r "$REQ"
git submodule update --init --recursive -q

# ── 2. models + benchmark data ───────────────────────────────────────────────
bash scripts/download_models.sh "$PRISM_PROFILE"
if [ ! -d "$DATA_DIR" ]; then
  log "downloading FDB-v3 audio"
  mkdir -p bench/data
  [ -f bench/data/fdb_v3.zip ] || uvx gdown "$DATA_GDRIVE_ID" -O bench/data/fdb_v3.zip
  (cd bench/data && unzip -q -o fdb_v3.zip)
  [ -d "$DATA_DIR" ] || DATA_DIR="$(dirname "$(find bench/data -name metadata.json | head -1)")/.." && DATA_DIR="$(cd "$DATA_DIR" && pwd)"
fi
[ -d "$DATA_DIR" ] || die "benchmark data not found under bench/data"

# ── 3. start LLM server + agent worker ───────────────────────────────────────
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT
MODEL="$(python -c 'from agent.config import load_config; print(load_config()["llm"]["model"])')"
BASE="$(python -c 'from agent.config import load_config; print(load_config()["llm"]["base_url"])')"
if [ "$PRISM_PROFILE" = "local-mac" ]; then
  log "starting mlx_lm.server ($MODEL)"
  python -m mlx_lm server --model "$MODEL" --port 8081 --prompt-cache-size 8 \
    --chat-template-args '{"enable_thinking": false}' >"$OUT/llm_server.log" 2>&1 & PIDS+=($!)
elif [ "$PRISM_PROFILE" = "local-cuda" ]; then
  log "starting vLLM ($MODEL)"
  python -m vllm.entrypoints.openai.api_server --model "$MODEL" --port 8000 --max-model-len 8192 \
    --gpu-memory-utilization 0.80 --seed 7 --enable-prefix-caching >"$OUT/llm_server.log" 2>&1 & PIDS+=($!)
fi
if [[ "$BASE" == http://127.0.0.1* ]]; then
  for _ in $(seq 1 180); do curl -sf "$BASE/models" >/dev/null && break; sleep 2; done
  curl -sf "$BASE/models" >/dev/null || die "LLM server did not come up; see $OUT/llm_server.log"
fi

: > /tmp/agent_tool_calls.log     # fresh telemetry for this run
: > /tmp/agent_heartbeat.log
export PRISM_TRACE_DIR="$OUT/traces"
log "starting agent worker"
python -m agent.main start >"$OUT/agent.log" 2>&1 & PIDS+=($!)
for _ in $(seq 1 90); do grep -qi "registered worker" "$OUT/agent.log" && break; sleep 2; done
grep -qi "registered worker" "$OUT/agent.log" || die "agent did not register with LiveKit; see $OUT/agent.log"

# ── 4. inference (benchmark's own client, unmodified) ────────────────────────
RUN_DIR="$DATA_DIR"
if [ -n "${LIMIT:-}" ]; then
  RUN_DIR="$ROOT/bench/data/subset_$LIMIT"; rm -rf "$RUN_DIR"; mkdir -p "$RUN_DIR"
  for d in $(ls "$DATA_DIR" | sort | head -n "$LIMIT"); do cp -R "$DATA_DIR/$d" "$RUN_DIR/"; done
fi
log "running FDB-v3 inference on $(ls "$RUN_DIR" | wc -l | tr -d ' ') examples"
python scripts/fdb_infer.py --provider "$PROVIDER" --root_dir "$RUN_DIR" --force 2>&1 | tee "$OUT/inference.log"

# ── 5. evaluation (benchmark's own evaluators) ───────────────────────────────
log "evaluating (judge=$JUDGE)"
JUDGE="$JUDGE" python scripts/fdb_eval.py --provider "$PROVIDER" --results-dir "$RUN_DIR" --out "$OUT" 2>&1 | tee "$OUT/eval.log"

# ── 6. collect ───────────────────────────────────────────────────────────────
cp /tmp/agent_tool_calls.log /tmp/agent_heartbeat.log "$OUT/" 2>/dev/null || true
cp agent/config.yaml "$OUT/config.yaml"
mkdir -p "$OUT/per_example"
for d in "$RUN_DIR"/*/; do n="$(basename "$d")"; cp "$d/result_$PROVIDER.json" "$OUT/per_example/$n.json" 2>/dev/null || true; done
python scripts/summarize.py "$OUT" | tee "$OUT/SUMMARY.md"
log "done: $OUT"
