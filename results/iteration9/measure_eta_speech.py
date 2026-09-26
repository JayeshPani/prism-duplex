"""Frozen four-case ABBA Kokoro→Parakeet probe; no downloads, LLM or RTC.

An external process deadline must supervise native inference and pool shutdown.
Signals preserve partial observations when Python regains control; no native
interruptibility or bounded thread-join claim is made.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import re
import signal
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.model_assets import file_sha256
from scripts.local_speech_experiment import memory, resample, timed, write_wav


def stamp():
    return datetime.now(timezone.utc).isoformat()


def hashes(expected, root):
    records = {}
    for name, digest in expected.items():
        try:
            observed = file_sha256(root / name)
            records[name] = {"expected": digest, "observed": observed, "matches": observed == digest}
        except Exception as exc:
            records[name] = {"expected": digest, "matches": False, "error": repr(exc)}
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    args = parser.parse_args()
    method_path, output = args.protocol.resolve(), args.out.resolve()
    method_bytes = method_path.read_bytes()
    method = json.loads(method_bytes)
    cases = method["cases"]
    if (method["arm_order"] != ["A", "B", "B", "A"] or len(cases) != 4
            or len({case["id"] for case in cases}) != 4
            or any(not re.fullmatch(r"[a-z0-9_-]+", case["id"])
                   or set(case["texts"]) != {"A", "B"}
                   or any(not isinstance(text, str) or not text.strip() for text in case["texts"].values())
                   or type(case["expected_minutes"]) is not int or case["expected_minutes"] < 1 for case in cases)):
        parser.error("declare four unique cases with safe ids, nonempty A/B texts, positive integer expected_minutes and ABBA order")
    output.mkdir(parents=True, exist_ok=False)
    (output / "raw").mkdir()
    (output / "protocol.json").write_bytes(method_bytes)
    os.environ.update(HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1",
                      PRISM_MODEL_RECEIPTS_DIR=str(output / "model-receipts"))
    trials = [{"id": f"block-{block}-{arm}-{case['id']}", "block": block, "arm": arm,
               "case": case["id"], "text": case["texts"][arm], "expected_minutes": case["expected_minutes"],
               "status": "pending", "attempted": False}
              for block, arm in enumerate(method["arm_order"], 1) for case in cases]
    report = {"status": "running", "started_at_utc": stamp(), "command": sys.argv, "pid": os.getpid(),
              "parent_pid": os.getppid(), "python": sys.version, "platform": platform.platform(),
              "profile": "local-mac", "protocol_path": str(method_path),
              "protocol_sha256": file_sha256(output / "protocol.json"), "trials": trials,
              "model_load_seconds": {}, "packages": {}, "cleanup": [],
              "timing": "Wall-clock seconds. TTS uses production render including PCM conversion; STT uses production transcribe including its worker queue/temp WAV. Stage times exclude model hashing and saved artifacts; total trial time includes those artifact writes. No acoustic timing.",
              "limits": ["Four fixed development cases, sixteen serial ABBA observations; no retries, trimming or discarded warm-up. First inference remains cold.",
                         "Shared warm speech models, fixed order and unisolated background applications prevent population or capacity claims.",
                         "Whole synthesized utterances, no live transport, VAD, microphone, tools or LLM. ASR is a content proxy, not human intelligibility.",
                         "No automatic qualitative score; review every text/audio/transcript against the declared cases.",
                         "RSS and MLX peak memory overlap and must not be summed. Samples can miss peaks.",
                         "Native inference and pool joins may delay signals and process exit; an external supervisor must enforce a hard deadline."]}

    def save():
        temporary = output / "report.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "report.json")

    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    def sample_memory():
        try:
            return memory(mlx)
        except Exception as exc:
            return {"error": repr(exc)}

    save()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    mlx = stt = tts = snapshot = None
    active = None
    stage = "source_verification"
    frozen, model_files = method.get("frozen_sha256", {}), method.get("model_files_sha256", {})
    try:
        required = {str(Path(__file__).relative_to(ROOT)), "scripts/local_speech_experiment.py",
                    "agent/config.py", "agent/config.yaml", "agent/model_assets.py",
                    "agent/pipeline/local_stt.py", "agent/pipeline/local_tts.py"}
        if not required.issubset(frozen) or any(Path(name).is_absolute() or ".." in Path(name).parts for name in frozen):
            raise ValueError("frozen_sha256 must include runner, helper and production speech/config sources as repo-relative paths")
        if not model_files or any(not Path(name).is_absolute() for name in model_files):
            raise ValueError("model_files_sha256 must declare absolute speech-model file paths")
        report["source_hashes_before"] = hashes(frozen, ROOT)
        if not all(row["matches"] for row in report["source_hashes_before"].values()):
            raise ValueError("frozen source/input mismatch")
        stage = "offline_assets"
        from agent.config import load_config
        from agent.model_assets import KOKORO_FILES, kokoro_records, resolve_snapshot, snapshot_record
        from agent.pipeline.local_stt import LocalSTT
        from agent.pipeline.local_tts import KokoroTTS, MODELS_DIR
        cfg = load_config("local-mac")
        cfg = {"stt": cfg["stt"], "tts": cfg["tts"]}
        if cfg != method["configuration"] or cfg["stt"]["kind"] != "parakeet-mlx" or cfg["tts"]["kind"] != "kokoro":
            raise ValueError("effective local-mac speech configuration differs from protocol")
        report["configuration"] = cfg
        snapshot = resolve_snapshot(cfg["stt"]["model"], cfg["stt"]["revision"], local_files_only=True)
        inventory = {str(path) for path in snapshot.rglob("*") if path.is_file()}
        inventory.update(str(MODELS_DIR / name) for name in KOKORO_FILES)
        report["model_inventory_matches_declaration"] = inventory == set(model_files)
        if inventory != set(model_files):
            raise ValueError("declare every Parakeet snapshot file and both production Kokoro assets, with no unrelated models")
        report["model_hashes_before"] = hashes(model_files, Path("/"))
        if not all(row["matches"] for row in report["model_hashes_before"].values()):
            raise ValueError("frozen speech model file mismatch")
        report["stt_model"] = snapshot_record(cfg["stt"]["model"], cfg["stt"]["revision"], snapshot)
        report["tts_assets"] = kokoro_records(MODELS_DIR)
        for name in ("mlx", "parakeet-mlx", "kokoro-onnx", "onnxruntime", "livekit", "livekit-agents",
                     "huggingface-hub", "numpy", "soundfile", "psutil", "espeakng-loader", "phonemizer-fork"):
            try:
                report["packages"][name] = version(name)
            except PackageNotFoundError:
                report["packages"][name] = None
        import numpy as np
        import mlx.core as mlx
        mlx.reset_peak_memory()
        report["memory_before_load"] = sample_memory()
        tts = KokoroTTS(voice=cfg["tts"]["voice"], speed=cfg["tts"]["speed"])
        stt = LocalSTT(**cfg["stt"])
        stage = "tts_load"
        timed(report["model_load_seconds"], "tts", tts.load)
        for asset in report["tts_assets"].values():
            asset["runtime_loaded"] = True
        report["memory_after_tts_load"] = sample_memory()
        save()
        stage = "stt_load"
        timed(report["model_load_seconds"], "stt", stt.load)
        report["stt_model"]["runtime_loaded_revision"] = snapshot.name
        report["memory_after_stt_load"] = sample_memory()
        save()
        for active in trials:
            active.update(status="running", attempted=True, started_at_utc=stamp())
            save()
            started = time.perf_counter()
            try:
                stage = "tts"
                pcm24 = timed(active, "tts_seconds", tts.render, active["text"])
                raw = output / "raw" / f"{active['id']}-24k.pcm"
                raw.write_bytes(pcm24)
                active["pcm24"] = {"file": str(raw.relative_to(output)), "sha256": file_sha256(raw),
                                   "samples": len(pcm24) // 2, "duration_seconds": len(pcm24) / 48000}
                for asset in report["tts_assets"].values():
                    asset["inference_observed"] = True
                save()
                if not pcm24 or len(pcm24) % 2:
                    raise ValueError("production TTS returned empty or malformed mono PCM16")
                wav24 = raw.with_suffix(".wav")
                write_wav(wav24, pcm24, 24000)
                active["wav24"] = {"file": str(wav24.relative_to(output)), "sha256": file_sha256(wav24)}
                active["memory_after_tts"] = sample_memory()
                stage = "resample"
                pcm16 = timed(active, "resample_seconds", resample, pcm24)
                wav16 = output / "raw" / f"{active['id']}-16k.wav"
                write_wav(wav16, pcm16, 16000)
                active["wav16"] = {"file": str(wav16.relative_to(output)), "sha256": file_sha256(wav16),
                                   "samples": len(pcm16) // 2, "duration_seconds": len(pcm16) / 32000}
                save()
                stage = "stt"
                active["transcript"] = timed(active, "stt_seconds", stt.transcribe, np.frombuffer(pcm16, dtype=np.int16))
                active["status"] = "completed"
                report["stt_model"]["inference_observed"] = True
            except Exception as exc:
                active.update(status="error", failed_stage=stage, error=repr(exc), traceback=traceback.format_exc())
            finally:
                active.update(total_trial_seconds=time.perf_counter() - started, observation_ended_at_utc=stamp(),
                              memory_after=sample_memory())
                save()
            print(json.dumps({key: active.get(key) for key in ("id", "status", "text", "transcript")}), flush=True)
    except BaseException as exc:
        report.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                      failed_stage=stage, error=repr(exc), traceback=traceback.format_exc())
        if active is not None and active["status"] == "running":
            active.update(status=report["status"], failed_stage=stage, error=repr(exc))
    finally:
        for trial in trials:
            if trial["status"] == "pending":
                trial.update(status="not_attempted", reason=report["status"])
        report["execution_ended_at_utc"] = stamp()
        save()  # Persist incomplete rows before any potentially slow native join.
        for name, pool in (("stt", getattr(getattr(stt, "_engine", None), "pool", None)),
                           ("tts", getattr(tts, "_pool", None))):
            if pool is None:
                continue
            record = {"pool": name, "status": "running", "started_at_utc": stamp()}
            report["cleanup"].append(record)
            save()
            try:
                timed(record, "seconds", pool.shutdown, True)
                record["status"] = "completed"
            except BaseException as exc:
                record.update(status="error", error=repr(exc))
            record["finished_at_utc"] = stamp()
            save()
        report["source_hashes_after"] = hashes(frozen, ROOT)
        report["model_hashes_after"] = hashes(model_files, Path("/"))
        report["protocol_hashes_after"] = hashes({str(method_path): report["protocol_sha256"],
                                                  str(output / "protocol.json"): report["protocol_sha256"]}, Path("/"))
        report["all_declared_hashes_match_after"] = all(record["matches"] for group in
            (report["source_hashes_after"], report["model_hashes_after"], report["protocol_hashes_after"]) for record in group.values())
        if snapshot is not None:
            report["model_inventory_unchanged"] = ({str(path) for path in snapshot.rglob("*") if path.is_file()}
                | {str(MODELS_DIR / name) for name in KOKORO_FILES}) == set(model_files)
        completed = sum(trial["status"] == "completed" for trial in trials)
        report["summary"] = {"declared": 16, "attempted": sum(trial["attempted"] for trial in trials),
                             "completed": completed, "failed_or_not_completed": 16 - completed,
                             "status_counts": dict(Counter(trial["status"] for trial in trials))}
        if report["status"] == "running":
            valid = (completed == 16 and report["all_declared_hashes_match_after"]
                     and report.get("model_inventory_unchanged") is True
                     and all(record["status"] == "completed" for record in report["cleanup"]))
            report["status"] = "completed" if valid else "completed_with_errors"
        report.update(finished_at_utc=stamp(), memory_after_cleanup=sample_memory())
        save()
    print(json.dumps({"report": str(output / "report.json"), "status": report["status"], **report["summary"]}), flush=True)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
