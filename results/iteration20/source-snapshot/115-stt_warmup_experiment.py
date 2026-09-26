"""Four offline, fresh-process Parakeet trials: baseline, warm, warm, baseline.

Warm trials transcribe one second of PCM silence before the same saved synthetic
speech. No model downloads, TTS, LLM, live audio, or production setting changes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import traceback
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Must precede imports of Hub/model packages, including in fresh children.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

from agent.model_assets import file_sha256, resolve_snapshot
from scripts.local_speech_experiment import memory, score, timed

ORDER = ("baseline", "warm", "warm", "baseline")


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def child(output: Path, mode: str) -> int:
    manifest_path = output.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    report = {"mode": mode, "status": "running", "pid": os.getpid(), "stage": "imports",
              "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "manifest_sha256": file_sha256(manifest_path), "timing_seconds": {},
              "warmup_transcript": None, "transcript": None, "memory": {}}
    path = output / "report.json"
    save(path, report)
    mlx = None
    try:
        for source, expected in manifest["source_sha256"].items():
            if file_sha256(ROOT / source) != expected:
                raise ValueError(f"source changed since manifest: {source}")
        for asset, record in manifest["model_assets"].items():
            stat = Path(asset).stat()
            if (stat.st_size, stat.st_mtime_ns) != (record["size_bytes"], record["mtime_ns"]):
                raise ValueError(f"model asset changed after verified hashing: {asset}")
        started = time.perf_counter()
        import numpy as np
        import mlx.core as mlx
        from agent.pipeline.local_stt import LocalSTT
        from agent.model_assets import resolve_snapshot
        report["timing_seconds"]["imports"] = time.perf_counter() - started
        report["packages"] = {p: version(p) for p in ("mlx", "parakeet-mlx", "numpy", "livekit-agents")}
        configuration = manifest["configuration"]
        snapshot = resolve_snapshot(configuration["model"], configuration["revision"], local_files_only=True)
        if str(snapshot) != manifest["snapshot_path"]:
            raise ValueError("resolved model snapshot differs from verified manifest")
        report["runtime_snapshot"] = str(snapshot)
        audio = Path(manifest["audio"]["path"])
        if file_sha256(audio) != manifest["audio"]["sha256"]:
            raise ValueError("speech audio changed since manifest")
        with wave.open(str(audio), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
                raise ValueError("expected 16 kHz mono PCM16 speech")
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        mlx.reset_peak_memory()
        report["memory"]["before_load"] = memory(mlx)
        stt = LocalSTT(**configuration)
        report["stage"] = "load"
        save(path, report)
        timed(report["timing_seconds"], "load", stt.load)
        report["memory"]["after_load"] = memory(mlx)
        report["runtime_loaded_revision"] = snapshot.name
        if mode == "warm":
            report["stage"] = "silence_warmup"
            save(path, report)
            report["warmup_transcript"] = timed(report["timing_seconds"], "silence_warmup",
                                                stt.transcribe, np.zeros(16000, dtype=np.int16))
            report["memory"]["after_warmup"] = memory(mlx)
        report["stage"] = "first_speech"
        save(path, report)
        report["transcript"] = timed(report["timing_seconds"], "first_speech", stt.transcribe, pcm)
        report["memory"]["after_first_speech"] = memory(mlx)
        report.update(score(manifest["reference"], report["transcript"]))
        report["exact_text"] = report["transcript"] == manifest["reference"]
        report["status"] = "completed"
    except Exception as error:
        report.update(status="error", error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
    finally:
        report["memory"]["final"] = memory(mlx)
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save(path, report)
    return 0 if report["status"] == "completed" else 1


def experiment(output: Path, source_report: Path) -> int:
    original = json.loads(source_report.read_text())
    direct = next(case for case in original["cases"] if case["id"] == "direct")
    audio = (source_report.parent / direct["audio_16k"]["file"]).resolve()
    if file_sha256(audio) != direct["audio_16k"]["sha256"]:
        raise ValueError("source audio does not match original speech report")
    model = original["stt_model"]
    snapshot = resolve_snapshot(model["model"], model["declared_revision"], local_files_only=True)
    assets = {}
    for name, expected in original["stt_model"]["files_sha256"].items():
        asset = snapshot / name
        if file_sha256(asset) != expected:
            raise ValueError(f"model asset differs from original speech report: {name}")
        stat = asset.stat()
        assets[str(asset)] = {"sha256": expected, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    sources = ("scripts/stt_warmup_experiment.py", "scripts/local_speech_experiment.py",
               "agent/pipeline/local_stt.py", "agent/model_assets.py", "agent/config.yaml")
    manifest = {"order": ORDER, "child_deadline_seconds": 120,
                "source_report": {"path": str(source_report), "sha256": file_sha256(source_report)},
                "source_sha256": {name: file_sha256(ROOT / name) for name in sources},
                "audio": {"path": str(audio), "sha256": file_sha256(audio)},
                "reference": direct["reference"], "configuration": original["configuration"]["stt"],
                "snapshot_path": str(snapshot), "model_assets": assets,
                "hardware": {"os": platform.platform(), "architecture": platform.machine(), "cpu_count": os.cpu_count()},
                "python": sys.version, "clock": "time.perf_counter; production worker dispatch and temporary WAV included",
                "limitations": ["Two trials per group; descriptive results only, no statistical generalization",
                                "Fresh processes share OS/file/kernel caches; asset verification primes the file cache",
                                "Fixed baseline,warm,warm,baseline order does not remove order or competing-load confounds",
                                "One previously generated synthetic phrase; no human speech, microphone, transport, VAD, or tool/model accuracy claims",
                                "All warm-up outputs and failed/incomplete child trials are retained",
                                "RSS and MLX memory overlap and must not be summed"]}
    save(output / "manifest.json", manifest)
    results = []
    for index, mode in enumerate(ORDER, 1):
        trial = output / f"{index:02d}-{mode}"
        trial.mkdir()
        command = [sys.executable, str(Path(__file__).resolve()), "--out", str(trial), "--child", mode]
        observed = {"index": index, "mode": mode, "command": command, "timeout": False}
        started = time.perf_counter()
        with (trial / "process.log").open("w") as log:
            try:
                process = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=120)
                observed["exit_code"] = process.returncode
            except subprocess.TimeoutExpired:
                observed.update(timeout=True, exit_code=None)
        observed["process_seconds"] = time.perf_counter() - started
        save(trial / "process.json", observed)
        report_path = trial / "report.json"
        results.append({**observed, "report": json.loads(report_path.read_text()) if report_path.exists() else None})
        save(output / "results.json", {"trials": results})
        print(json.dumps({"index": index, "mode": mode, "exit_code": observed["exit_code"],
                          "report": results[-1]["report"]}), flush=True)
    summary = {"planned": 4, "completed": sum(r["exit_code"] == 0 for r in results), "groups": {}}
    for mode in ("baseline", "warm"):
        reports = [r["report"] for r in results if r["mode"] == mode and r["exit_code"] == 0]
        values = [r["timing_seconds"]["first_speech"] for r in reports]
        summary["groups"][mode] = {"completed": len(reports), "first_speech_seconds": values,
                                   "mean_first_speech_seconds": statistics.mean(values) if values else None,
                                   "exact_text": sum(r["exact_text"] for r in reports)}
    save(output / "summary.json", summary)
    return 0 if summary["completed"] == 4 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-report", type=Path)
    parser.add_argument("--child", choices=("baseline", "warm"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.out.resolve()
    if args.child:
        return child(output, args.child)
    if args.source_report is None:
        parser.error("--source-report is required")
    output.mkdir(parents=True, exist_ok=False)
    return experiment(output, args.source_report.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
