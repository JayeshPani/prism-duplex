"""Transcribe complete RTC receive recordings as an offline intelligibility proxy.

The protocol and input hashes are saved before model loading. No received samples
are trimmed, filtered, selected by loudness, or retried after inspecting output.
This is ASR evidence, not human listening or an independent speech-quality score.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.model_assets import file_sha256
from scripts.local_speech_experiment import memory, resample, timed, write_wav


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "resampled").mkdir()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["PRISM_MODEL_RECEIPTS_DIR"] = str(output / "model-receipts")
    trials = []
    for repeat in ("run1", "repeat2", "repeat3", "repeat4", "repeat5"):
        for kind in ("baseline", "correction"):
            source = args.input_root.resolve() / f"rtc-{kind}-{repeat}"
            capture = json.loads((source / "report.json").read_text())
            if len(capture["tracks"]) != 1:
                raise ValueError(f"Expected exactly one captured track: {source}")
            track = capture["tracks"][0]
            if (track["sample_rate"], track["channels"], track["sample_bytes"]) != (24000, 1, 2):
                raise ValueError(f"Unexpected capture format: {source}")
            trials.append({
                "id": source.name, "kind": kind, "source": str(source), "room": capture["room"],
                "pcm_file": track["file"],
                "input_sha256": {name: file_sha256(source / name)
                                 for name in ("report.json", "events.jsonl", "frames.jsonl", track["file"])},
                "declared_speech": capture["speech_handles"],
                "status": "pending", "attempted": False,
            })
    protocol = {
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": sys.argv, "script_sha256": file_sha256(Path(__file__)),
        "scope": "One complete untrimmed received PCM stream per trial; ten sequential offline transcriptions",
        "trial_order": [trial["id"] for trial in trials],
        "inputs": [{key: trial[key] for key in ("id", "source", "pcm_file", "input_sha256")} for trial in trials],
        "method": "Production LocalSTT.load/transcribe; pinned local-mac Parakeet; LiveKit whole-stream 24-to-16 kHz resampling",
        "warmup": "None; first inference retained; no retries or transcript-conditioned clipping",
        "review": {
            "baseline": "Inspect complete transcript for acknowledgement, airport navigation, and 88-minute ETA; preserve any mismatches",
            "correction": "Expect acknowledgements, possibly partial deliberately interrupted airport speech, then office navigation and replacement of airport; do not expect the complete old result",
            "scoring": "Report verbatim ASR output with qualitative fact review; no benchmark score or exact-match pass rate",
        },
        "limitations": [
            "ASR proxy is not human listening, acoustic quality measurement, or an independent TTS judge",
            "The same pinned ASR model is used in production; recognition errors and pronunciation errors are not separable here",
            "All captured silence is retained; missing inter-frame wall-clock gaps are not inserted into concatenated PCM",
            "Whole-stream text does not establish word-to-event or RTP/data timing attribution",
            "Synthetic user input, one voice, ten dependent development trials, no microphone/speaker echo path",
            "Review inference timings exclude the live experiment and are not conversational latency measurements",
        ],
    }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    report = {
        "status": "running", "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": file_sha256(output / "protocol.json"), "trials": trials,
        "python": sys.version, "platform": platform.platform(), "packages": {},
        "source_sha256": {str(path.relative_to(ROOT)): file_sha256(path)
                          for path in sorted((ROOT / "agent").rglob("*.py"))},
        "model_load_seconds": {},
    }
    for path in (ROOT / "agent/config.yaml", Path(__file__), ROOT / "scripts/local_speech_experiment.py"):
        report["source_sha256"][str(path.relative_to(ROOT))] = file_sha256(path)

    def save():
        temporary = output / "report.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "report.json")

    save()
    stt = None
    mlx = None
    stage = "imports_and_assets"
    try:
        import numpy as np
        import mlx.core as mlx
        from agent.config import load_config
        from agent.model_assets import resolve_snapshot, snapshot_record
        from agent.pipeline.local_stt import LocalSTT
        for package in ("mlx", "parakeet-mlx", "livekit", "livekit-agents", "huggingface-hub", "numpy", "soundfile", "psutil"):
            try:
                report["packages"][package] = version(package)
            except PackageNotFoundError:
                pass
        cfg = load_config("local-mac")["stt"]
        report["stt_config"] = cfg
        snapshot = resolve_snapshot(cfg["model"], cfg["revision"], local_files_only=True)
        report["stt_model"] = snapshot_record(cfg["model"], cfg["revision"], snapshot)
        report["stt_model"]["files_sha256"] = {
            str(path.relative_to(snapshot)): file_sha256(path)
            for path in sorted(snapshot.rglob("*")) if path.is_file()}
        mlx.reset_peak_memory()
        report["memory_before_load"] = memory(mlx)
        stt = LocalSTT(**cfg)
        stage = "model_load"
        timed(report["model_load_seconds"], "stt", stt.load)
        report["stt_model"]["runtime_loaded_revision"] = snapshot.name
        report["memory_after_load"] = memory(mlx)
        save()
        for trial in trials:
            trial.update(status="running", attempted=True)
            save()
            try:
                stage = "resample"
                source = Path(trial["source"]) / trial["pcm_file"]
                pcm24 = source.read_bytes()
                if len(pcm24) % 2 or file_sha256(source) != trial["input_sha256"][trial["pcm_file"]]:
                    raise ValueError("PCM is malformed or changed after protocol freeze")
                trial["received_samples"] = len(pcm24) // 2
                trial["received_duration_seconds"] = len(pcm24) / 48000
                pcm16 = timed(trial, "resample_seconds", resample, pcm24)
                wav = output / "resampled" / f"{trial['id']}.wav"
                write_wav(wav, pcm16, 16000)
                trial["resampled"] = {"file": str(wav.relative_to(output)), "sha256": file_sha256(wav),
                                      "samples": len(pcm16) // 2, "duration_seconds": len(pcm16) / 32000}
                stage = "transcribe"
                trial["transcript"] = timed(trial, "stt_seconds", stt.transcribe, np.frombuffer(pcm16, dtype=np.int16))
                trial["status"] = "completed"
                report["stt_model"]["inference_observed"] = True
            except Exception as exc:
                trial.update(status="error", failed_stage=stage, error=f"{type(exc).__name__}: {exc}",
                             traceback=traceback.format_exc())
            trial["memory_after"] = memory(mlx)
            save()
            print(json.dumps({key: trial.get(key) for key in ("id", "status", "transcript", "stt_seconds")}), flush=True)
        report["inputs_unchanged"] = all(
            file_sha256(Path(trial["source"]) / name) == digest
            for trial in trials for name, digest in trial["input_sha256"].items())
        report["source_unchanged"] = all(file_sha256(ROOT / name) == digest
                                         for name, digest in report["source_sha256"].items())
    except Exception as exc:
        report.update(status="error", failed_stage=stage, error=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc())
    finally:
        if stt is not None:
            stt._engine.pool.shutdown(wait=True)
        for trial in trials:
            if trial["status"] == "pending":
                trial.update(status="not_attempted", reason="setup_or_model_load_failed")
        completed = sum(trial["status"] == "completed" for trial in trials)
        report["summary"] = {"planned": len(trials), "attempted": sum(trial["attempted"] for trial in trials),
                             "completed": completed, "failed_or_not_completed": len(trials) - completed}
        if report["status"] == "running":
            report["status"] = "completed" if completed == len(trials) else "completed_with_errors"
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({"status": report["status"], "report": str(output / "report.json"), **report["summary"]}), flush=True)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
