#!/usr/bin/env bash
# Fetch pinned local model weights for a profile (idempotent; resumes partial downloads).
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE="${1:-local-mac}"
mkdir -p models/kokoro
K=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
[ -s models/kokoro/kokoro-v1.0.onnx ] || curl -fL -o models/kokoro/kokoro-v1.0.onnx "$K/kokoro-v1.0.onnx"
[ -s models/kokoro/voices-v1.0.bin ] || curl -fL -o models/kokoro/voices-v1.0.bin "$K/voices-v1.0.bin"
MODEL="$(python -c 'from agent.config import load_config; print(load_config()["llm"]["model"])')"
STT="$(python -c 'from agent.config import load_config; print(load_config()["stt"]["model"])')"
case "$PROFILE" in
  local-mac)  hf download "$MODEL" >/dev/null; hf download "$STT" >/dev/null ;;
  local-cuda) hf download "$MODEL" >/dev/null; hf download "$STT" >/dev/null ;;
  *) : ;;  # hosted LLM profiles still need the local STT
esac
echo "[prism] models ready for $PROFILE"
