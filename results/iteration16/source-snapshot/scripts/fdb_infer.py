"""Run FDB-v3 batch inference against the running PRISM agent.

Thin wrapper around the benchmark's own run_tool_benchmark_all_released.py.
The benchmark files are used unmodified. Model loading is bound to declared
snapshots. On machines
without CUDA (e.g. Apple Silicon dev laptops) the benchmark's NeMo Parakeet
ASR - which calls .cuda() - is replaced by the same checkpoint
(parakeet-tdt-0.6b-v2) running on MLX, returning the identical output format.
On CUDA only the model loader is replaced with the pinned NeMo checkpoint;
the benchmark's decoding and timestamp code remains unchanged.

    python scripts/fdb_infer.py --provider prism --root_dir bench/data/fdb_v3_data_released [--force]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V3 = ROOT / "bench" / "Full-Duplex-Bench" / "v3"
sys.path.insert(0, str(V3))
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # benchmark loads ".env.local" from the cwd

from agent.model_assets import PARAKEET_REVISIONS, record_loaded_model, resolve_snapshot


def _cuda() -> bool:
    try:
        import torch  # noqa: F401
        return torch.cuda.is_available()
    except Exception:
        return False


def _patch_asr_with_mlx() -> None:
    import run_tool_benchmark as rtb  # type: ignore
    import run_tool_benchmark_all_released as rel  # type: ignore

    def load_asr_model():
        from parakeet_mlx import from_pretrained
        print("🔊 ASR: parakeet-tdt-0.6b-v2 on MLX (no CUDA on this machine)")
        repo = "mlx-community/parakeet-tdt-0.6b-v2"
        revision = PARAKEET_REVISIONS[repo]
        path = resolve_snapshot(repo, revision)
        model = from_pretrained(str(path))
        record_loaded_model("evaluation_asr", repo, revision, path, "parakeet-mlx")
        return model

    def run_asr(model, audio_path):
        try:
            res = model.transcribe(str(audio_path))
            chunks, word, start, end = [], "", None, None
            for sent in res.sentences:
                for tok in sent.tokens:
                    t = tok.text
                    if t.startswith(" ") and word:
                        chunks.append({"text": word, "timestamp": [start, end]})
                        word, start = "", None
                    word += t.strip() if not word else t
                    start = tok.start if start is None else start
                    end = tok.end
            if word:
                chunks.append({"text": word, "timestamp": [start, end]})
            return {"text": res.text.strip(), "chunks": chunks}
        except Exception as e:
            print(f"  ❌ ASR error: {e}")
            return {"text": "", "chunks": [], "error": str(e)}

    rtb.load_asr_model = load_asr_model
    rtb.run_asr = run_asr
    rel.load_asr_model = load_asr_model


def _pin_nemo_asr() -> None:
    import run_tool_benchmark as rtb  # type: ignore
    import run_tool_benchmark_all_released as rel  # type: ignore

    def load_asr_model():
        import nemo.collections.asr as nemo_asr
        repo = "nvidia/parakeet-tdt-0.6b-v2"
        revision = PARAKEET_REVISIONS[repo]
        path = resolve_snapshot(repo, revision)
        model = nemo_asr.models.ASRModel.restore_from(
            restore_path=str(path / "parakeet-tdt-0.6b-v2.nemo")).cuda()
        record_loaded_model("evaluation_asr", repo, revision, path, "nemo-parakeet")
        return model

    rtb.load_asr_model = load_asr_model
    rel.load_asr_model = load_asr_model


if __name__ == "__main__":
    import run_tool_benchmark_all_released as rel  # type: ignore

    if os.getenv("PRISM_ASR", "auto") == "mlx" or (os.getenv("PRISM_ASR", "auto") == "auto" and not _cuda()):
        _patch_asr_with_mlx()
    else:
        _pin_nemo_asr()
    rel.main()
