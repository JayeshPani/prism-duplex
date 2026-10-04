"""Resolve declared Hub snapshots before passing local paths to model loaders.

Heavy inference and Hub packages are deliberately imported only by callers that
need them. A snapshot selection is not evidence that inference has occurred.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory

PARAKEET_REVISIONS = {
    "mlx-community/parakeet-tdt-0.6b-v2": "8ae155301e23d820d82aa60d24817c900e69e487",
    "nvidia/parakeet-tdt-0.6b-v2": "ae9ad07059c7c739ffaf932226a8fe64ae2620b0",
}
KOKORO_SOURCE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
# Freeze the bytes observed in PRISM's 2026-09-25 local speech experiment.
# These are project reference hashes, not authenticated publisher digests.
KOKORO_REFERENCE_SHA256 = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}


def validate_revision(model: str, revision: str | None, *, allow_unfrozen: bool = False) -> None:
    if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"{model}: revision must be a full immutable 40-character Hub commit SHA")
    if revision is None and not allow_unfrozen:
        raise ValueError(f"{model}: model revision required; unfrozen models are development-only")


def resolve_snapshot(model: str, revision: str | None, *, allow_unfrozen: bool = False,
                     local_files_only: bool = False) -> Path:
    validate_revision(model, revision, allow_unfrozen=allow_unfrozen)
    from huggingface_hub import snapshot_download
    path = Path(snapshot_download(repo_id=model, revision=revision,
                                  local_files_only=local_files_only)).resolve()
    # Hub's snapshot directory is named with the resolved commit. Never silently
    # accept a moved branch or the snapshot of a different declared revision.
    if not re.fullmatch(r"[0-9a-f]{40}", path.name) or (revision and path.name != revision):
        raise ValueError(f"{model}: resolved snapshot does not match declared revision {revision}")
    return path


def snapshot_record(model: str, revision: str | None, path: Path) -> dict:
    return {"model": model, "declared_revision": revision, "resolved_revision": path.name,
            "snapshot_path": str(path), "reproducibility": "frozen" if revision else "unfrozen_development",
            "runtime_loaded_revision": None, "inference_observed": False}


def record_loaded_model(component: str, model: str, revision: str | None, path: Path,
                        backend: str) -> dict:
    """Call only after the real loader succeeds, not after download or discovery."""
    record = {**snapshot_record(model, revision, path), "component": component,
              "backend": backend, "runtime_loaded_revision": path.name,
              "evidence": "local_loader_returned", "pid": os.getpid(),
              "loaded_at_utc": datetime.now(timezone.utc).isoformat()}
    if directory := os.getenv("PRISM_MODEL_RECEIPTS_DIR"):
        dest = Path(directory)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f"{component}-{os.getpid()}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n")
        temporary.replace(target)
    return record


def load_stt_model(kind: str, model: str, revision: str | None, *, allow_unfrozen: bool = False):
    path = resolve_snapshot(model, revision, allow_unfrozen=allow_unfrozen)
    if kind == "parakeet-mlx":
        from parakeet_mlx import from_pretrained
        engine = from_pretrained(str(path))
    elif kind == "nemo-parakeet":
        import nemo.collections.asr as nemo_asr
        import torch
        checkpoints = list(path.glob("*.nemo"))
        if len(checkpoints) != 1:
            raise ValueError(f"Expected one NeMo checkpoint in {path}")
        engine = nemo_asr.models.ASRModel.restore_from(restore_path=str(checkpoints[0]))
        engine = engine.cuda() if torch.cuda.is_available() else engine
        engine.eval()
    else:
        raise ValueError(f"unknown STT kind {kind}")
    record_loaded_model("agent_stt", model, revision, path, kind)
    return engine


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def kokoro_records(directory: Path) -> dict:
    # The original release has no publisher digests (GitHub asset digest=null).
    # Local hashes identify observed bytes; they are not an authenticated pin.
    records = {}
    for name in KOKORO_FILES:
        observed = file_sha256(directory / name) if (directory / name).is_file() else None
        records[name] = {"source_url": f"{KOKORO_SOURCE}/{name}", "expected_sha256": None,
                         "observed_sha256": observed, "reference_sha256": KOKORO_REFERENCE_SHA256[name],
                         "reference_origin": "PRISM local-speech-run1 observed bytes, 2026-09-25; not a publisher digest",
                         "matches_reference": observed == KOKORO_REFERENCE_SHA256[name],
                         "source_verified": False, "runtime_loaded": False}
    return records


def verify_kokoro_assets(directory: Path) -> dict:
    """Reject missing or changed weights before loading native inference code."""
    records = kokoro_records(directory)
    for name, record in records.items():
        if record["observed_sha256"] is None:
            raise FileNotFoundError(f"Kokoro asset missing: {directory / name}; run scripts/download_models.sh")
        if not record["matches_reference"]:
            raise ValueError(f"Kokoro checksum mismatch for {directory / name}: expected PRISM reference "
                             f"{record['reference_sha256']}, observed {record['observed_sha256']}; "
                             "replace this file with the declared release asset before retrying")
    return records


def prepare_espeak_data(source: str | Path) -> tuple[str, TemporaryDirectory | None]:
    """Keep resolved data paths below eSpeak NG 1.52's 160-byte POSIX buffer.

    Phonemizer resolves symlinks, so a short alias cannot fix a long path. The
    caller must retain the returned temporary directory for the engine's life.
    """
    source = Path(source).resolve()
    if len(str(source).encode("utf-8")) < 160:
        return str(source), None
    temporary = TemporaryDirectory(prefix="prism-espeak-")
    try:
        destination = (Path(temporary.name) / "espeak-ng-data").resolve()
        if len(str(destination).encode("utf-8")) >= 160:
            raise RuntimeError("eSpeak data path is too long; set TMPDIR to a shorter directory")
        shutil.copytree(source, destination)
    except BaseException:
        temporary.cleanup()
        raise
    return str(destination), temporary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("llm", "stt"))
    parser.add_argument("--profile")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    from .config import load_config
    cfg = load_config(args.profile)[args.component]
    path = resolve_snapshot(cfg["model"], cfg.get("revision"),
                            allow_unfrozen=cfg.get("allow_unfrozen", False),
                            local_files_only=args.local_files_only)
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(snapshot_record(cfg["model"], cfg.get("revision"), path), indent=2) + "\n")
    print(path)


if __name__ == "__main__":
    main()
