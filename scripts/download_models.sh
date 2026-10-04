#!/usr/bin/env bash
# Fetch declared Hub snapshots and the original Kokoro release assets.
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE="${1:-local-mac}"
mkdir -p models/kokoro
K=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
for asset in kokoro-v1.0.onnx voices-v1.0.bin; do
  if [ ! -s "models/kokoro/$asset" ]; then
    # A failed download must not become an apparently complete model next run.
    curl -fL -o "models/kokoro/$asset.partial" "$K/$asset"
    mv "models/kokoro/$asset.partial" "models/kokoro/$asset"
  fi
done
python - <<'PY'
import json
from pathlib import Path
from agent.model_assets import verify_kokoro_assets
directory = Path("models/kokoro")
(directory / "observed_assets.json").write_text(json.dumps(verify_kokoro_assets(directory), indent=2) + "\n")
PY
case "$PROFILE" in
  local-mac|local-cuda) python -m agent.model_assets llm --profile "$PROFILE" >/dev/null ;;
esac
# Hosted LLM profiles still need the pinned local STT.
python -m agent.model_assets stt --profile "$PROFILE" >/dev/null
echo "[prism] models ready for $PROFILE"
