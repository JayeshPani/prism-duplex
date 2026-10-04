"""Offline Kokoro → Parakeet development probe using synthetic navigation audio.

Requires the selected profile's installed dependencies and already cached model
files. Never downloads models. This measures neither human speech nor live audio,
VAD, barge-in, microphone echo, tool accuracy, or a benchmark score.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import re
import resource
import sys
import time
import traceback
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASES = (
    ("direct", "Navigate to Maple Library."),
    ("correction", "Take me to Brookfield Station. Actually, make that Cedar Market."),
    ("ambiguous_place", "Navigate to Riverside."),
    ("place_and_number", "Take me to West Harbor Garage, level two."),
)


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def score(reference: str, transcript: str) -> dict:
    expected, actual = tokens(reference), tokens(transcript)
    previous = list(range(len(actual) + 1))
    for i, word in enumerate(expected, 1):
        current = [i]
        for j, observed in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (word != observed)))
        previous = current
    return {"normalized_reference": " ".join(expected), "normalized_transcript": " ".join(actual),
            "exact_normalized": expected == actual, "word_edits": previous[-1],
            "reference_words": len(expected), "wer": previous[-1] / len(expected)}


def timed(record: dict, key: str, function, *args):
    started = time.perf_counter()
    try:
        return function(*args)
    finally:
        record[key] = time.perf_counter() - started


def memory(mlx=None) -> dict:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {"process_peak_rss_bytes": peak * (1 if sys.platform == "darwin" else 1024)}
    try:
        import psutil
        result["process_rss_bytes"] = psutil.Process().memory_info().rss
    except ImportError:
        result["process_rss_bytes"] = None
    result["mlx_peak_bytes"] = mlx.get_peak_memory() if mlx is not None else None
    return result


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def resample(pcm: bytes) -> bytes:
    from livekit import rtc
    frame = rtc.AudioFrame(pcm, sample_rate=24000, num_channels=1, samples_per_channel=len(pcm) // 2)
    converter = rtc.AudioResampler(24000, 16000, num_channels=1)
    return bytes(rtc.combine_audio_frames(converter.push(frame) + converter.flush()).data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New output directory; existing directories are refused")
    parser.add_argument("--profile", choices=("local-mac", "local-cuda"), default="local-mac")
    args = parser.parse_args()
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "raw").mkdir()
    # Set before importing the Hub or model libraries. The production loader
    # still resolves the declared SHA, and missing cached weights fail locally.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["PRISM_MODEL_RECEIPTS_DIR"] = str(output / "model_receipts")
    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "command": sys.argv,
        "status": "running", "profile": args.profile,
        "scope": "Independent development text rendered by the same local Kokoro voice, then transcribed locally; synthetic audio only",
        "limitations": ["No human speech, live transport, VAD, interruption, microphone, tool execution, or benchmark evaluation",
                        "Four single trials; no statistical latency or accuracy generalization",
                        "Timing includes the first cold inference; no warm-up or discarded cases",
                        "This process loads speech models only; system-wide competing workloads are not measured",
                        "RSS and MLX memory are different overlapping measurements; do not sum them"],
        "normalization": "ASCII lowercase alphanumeric tokens; punctuation separates tokens; no number spelling, synonym, or entity normalization",
        "timing": "Wall-clock seconds; TTS includes PCM conversion; STT includes its production worker and temporary WAV; excludes asset hashing",
        "hardware": {"os": platform.platform(), "architecture": platform.machine(), "processor": platform.processor(),
                     "cpu_count": os.cpu_count()},
        "python": sys.version, "model_load_seconds": {}, "packages": {},
        "cases": [{"id": name, "reference": text, "status": "pending", "attempted": False}
                  for name, text in CASES],
    }

    def save():
        temporary = output / "report.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "report.json")

    save()
    mlx = None
    stage = "imports_and_assets"
    try:
        import numpy as np
        from agent.config import load_config
        from agent.model_assets import file_sha256, kokoro_records, resolve_snapshot, snapshot_record
        from agent.pipeline.local_stt import LocalSTT
        from agent.pipeline.local_tts import KokoroTTS, MODELS_DIR
        report["harness_sha256"] = file_sha256(Path(__file__))
        report["source_sha256"] = {str(path.relative_to(ROOT)): file_sha256(path)
                                   for path in sorted((ROOT / "agent").rglob("*.py"))}
        report["source_sha256"]["agent/config.yaml"] = file_sha256(ROOT / "agent/config.yaml")
        for package in ("mlx", "parakeet-mlx", "kokoro-onnx", "onnxruntime", "livekit", "livekit-agents",
                        "huggingface-hub", "numpy", "soundfile", "psutil", "nemo_toolkit", "torch",
                        "espeakng-loader", "phonemizer-fork"):
            try:
                report["packages"][package] = version(package)
            except PackageNotFoundError:
                pass
        cfg = load_config(args.profile)
        report["configuration"] = {"stt": cfg["stt"], "tts": cfg["tts"]}
        snapshot = resolve_snapshot(cfg["stt"]["model"], cfg["stt"]["revision"], local_files_only=True)
        report["stt_model"] = snapshot_record(cfg["stt"]["model"], cfg["stt"]["revision"], snapshot)
        report["stt_model"]["files_sha256"] = {
            str(path.relative_to(snapshot)): file_sha256(path) for path in sorted(snapshot.rglob("*")) if path.is_file()}
        report["tts_assets"] = kokoro_records(MODELS_DIR)
        if cfg["stt"]["kind"] == "parakeet-mlx":
            import mlx.core as mlx
            mlx.reset_peak_memory()
        report["memory_before_load"] = memory(mlx)
        tts = KokoroTTS(voice=cfg["tts"]["voice"], speed=cfg["tts"]["speed"])
        stt = LocalSTT(**cfg["stt"])
        stage = "tts_load"
        timed(report["model_load_seconds"], "tts", tts.load)
        for asset in report["tts_assets"].values():
            asset["runtime_loaded"] = True
        report["memory_after_tts_load"] = memory(mlx)
        save()
        stage = "stt_load"
        timed(report["model_load_seconds"], "stt", stt.load)
        report["stt_model"]["runtime_loaded_revision"] = snapshot.name
        report["memory_after_stt_load"] = memory(mlx)
        save()
        for case in report["cases"]:
            case.update(status="running", attempted=True)
            save()
            try:
                stage = "tts"
                pcm24 = timed(case, "tts_seconds", tts.render, case["reference"])
                for asset in report["tts_assets"].values():
                    asset["inference_observed"] = True
                wav24 = output / "raw" / f"{case['id']}-tts-24k.wav"
                write_wav(wav24, pcm24, 24000)
                case["audio_24k"] = {"file": str(wav24.relative_to(output)), "sha256": file_sha256(wav24),
                                     "duration_seconds": len(pcm24) / 2 / 24000}
                case["memory_after_tts"] = memory(mlx)
                stage = "resample"
                pcm16 = timed(case, "resample_seconds", resample, pcm24)
                wav16 = output / "raw" / f"{case['id']}-stt-16k.wav"
                write_wav(wav16, pcm16, 16000)
                case["audio_16k"] = {"file": str(wav16.relative_to(output)), "sha256": file_sha256(wav16),
                                     "duration_seconds": len(pcm16) / 2 / 16000}
                save()
                stage = "stt"
                case["transcript"] = timed(case, "stt_seconds", stt.transcribe, np.frombuffer(pcm16, dtype=np.int16))
                case.update(score(case["reference"], case["transcript"]), status="completed")
                case["stt_realtime_factor"] = case["stt_seconds"] / case["audio_16k"]["duration_seconds"]
                report["stt_model"]["inference_observed"] = True
            except Exception as exc:
                case.update(status="error", failed_stage=stage, error=f"{type(exc).__name__}: {exc}",
                            traceback=traceback.format_exc())
            case["memory_after_case"] = memory(mlx)
            save()
    except Exception as exc:
        report.update(status="error", failed_stage=stage, error=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc())
        for case in report["cases"]:
            if case["status"] == "pending":
                case.update(status="not_attempted", reason="setup_or_model_load_failed")
    finally:
        completed = [case for case in report["cases"] if case["status"] == "completed"]
        reference_words = sum(case["reference_words"] for case in completed)
        report["summary"] = {"planned": len(CASES), "attempted": sum(case["attempted"] for case in report["cases"]),
                             "completed": len(completed), "failed_or_not_completed": len(CASES) - len(completed),
                             "exact_normalized": sum(case["exact_normalized"] for case in completed),
                             "exact_denominator": len(CASES), "wer_completed_only_denominator_words": reference_words,
                             "wer_completed_only": sum(case["word_edits"] for case in completed) / reference_words if reference_words else None}
        if report["status"] == "running":
            report["status"] = "completed" if len(completed) == len(CASES) else "completed_with_errors"
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({"report": str(output / "report.json"), "status": report["status"], **report["summary"]}, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
