"""Review every declared RTC capture through whole-stream, offline production ASR.

Run only after the suite and its owned services have stopped. No clipping,
retries, silence removal, or capture-status-based exclusion is performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import sys
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.model_assets import file_sha256
from scripts.local_speech_experiment import memory, resample, timed, write_wav


def stamp():
    return datetime.now(timezone.utc).isoformat()


def declared_trials(protocol):
    counts = Counter()
    for case in protocol["run_order"]:
        counts[case] += 1
        yield {"id": f"rtc-{case}-{counts[case]}", "case": case}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    base, output = args.suite_root.resolve(), args.out.resolve()
    suite_protocol = json.loads((base / "protocol.json").read_text())
    suite = json.loads((base / "run-report.json").read_text())
    from common import verify_frozen, absent
    verify_frozen()
    for arm_id in ('baseline','candidate'):
        closed = json.loads((ROOT/'results'/f'iteration18-{arm_id}'/'run-report.json').read_text())
        assert closed.get('finished_at') and closed['status'] not in {'starting','running'}
        owned = {r['pid'] for r in closed['runs']} | {r['pid'] for r in closed['cleanup']}
        assert all(absent(pid) for pid in owned), f'{arm_id} timed process remains'
    method_path = Path(__file__).with_name("received-audio-review-protocol.json")
    method = json.loads(method_path.read_text())
    if suite["status"] in {"starting", "running"} or any(
            role not in {r["role"] for r in suite.get("cleanup", []) if r.get("exit_code") is not None}
            for role in ("worker", "llm", "livekit")):
        parser.error("suite must be terminal with exit receipts for all three owned services")
    if method["script_sha256"] != file_sha256(Path(__file__)) or method["suite_protocol_sha256"][base.name] != file_sha256(base / "protocol.json"):
        parser.error("review script or suite protocol differs from the frozen review method")
    output.mkdir(parents=True, exist_ok=False)
    (output / "resampled").mkdir()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["PRISM_MODEL_RECEIPTS_DIR"] = str(output / "model-receipts")
    trials = list(declared_trials(suite_protocol))
    for trial in trials:
        source = base / trial["id"]
        trial.update(source=str(source), status="pending", attempted=False, input_sha256={})
        trial["suite_run_entries"] = [r for r in suite["runs"] if r["name"] == trial["id"]]
        try:
            capture = json.loads((source / "report.json").read_text())
            trial.update(capture_status=capture["status"], room=capture["room"],
                         declared_speech=capture["speech_handles"])
            if len(capture["tracks"]) != 1:
                raise ValueError("expected one received track; all track metadata remains in capture report")
            track = capture["tracks"][0]
            if (track["sample_rate"], track["channels"], track["sample_bytes"]) != (24000, 1, 2):
                raise ValueError("expected mono 24kHz PCM16 capture")
            trial["pcm_file"] = track["file"]
            trial["input_sha256"] = {name: file_sha256(source / name)
                                      for name in ("report.json", "events.jsonl", "frames.jsonl", track["file"])}
        except Exception as exc:
            trial.update(status="unavailable_capture", error=f"{type(exc).__name__}: {exc}")
    source_paths = [*sorted((ROOT / "agent").rglob("*.py")), ROOT / "agent/config.yaml",
                    Path(__file__), ROOT / "scripts/local_speech_experiment.py"]
    frozen = {"frozen_at_utc": stamp(), "command": sys.argv, "method": method,
              "method_sha256": file_sha256(method_path), "suite_status": suite["status"],
              "suite_sha256": {name: file_sha256(base / name) for name in ("protocol.json", "run-report.json")},
              "trial_order": [t["id"] for t in trials], "trials": trials,
              "source_sha256": {str(p.relative_to(ROOT)): file_sha256(p) for p in source_paths}}
    (output / "protocol.json").write_text(json.dumps(frozen, indent=2) + "\n")
    report = {"status": "running", "started_at_utc": stamp(), "protocol_sha256": file_sha256(output / "protocol.json"),
              "python": sys.version, "platform": platform.platform(), "source_sha256": frozen["source_sha256"],
              "trials": trials, "model_load_seconds": {},
              "suite_order_matches_declaration": [r["name"] for r in suite["runs"]] == frozen["trial_order"],
              "suite_cases_match_declaration": all(len(t["suite_run_entries"]) == 1 and
                  t["suite_run_entries"][0]["case"] == t["case"] for t in trials),
              "undeclared_suite_runs": [r for r in suite["runs"] if r["name"] not in frozen["trial_order"]]}

    def save():
        temporary = output / "report.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "report.json")

    save()
    stt = None
    stage = "imports_and_assets"
    try:
        import numpy as np
        import mlx.core as mlx
        from agent.config import load_config
        from agent.model_assets import resolve_snapshot, snapshot_record
        from agent.pipeline.local_stt import LocalSTT
        report["packages"] = {name: version(name) for name in (
            "mlx", "parakeet-mlx", "livekit", "livekit-agents", "huggingface-hub", "numpy", "soundfile", "psutil")}
        cfg = load_config("local-mac")["stt"]
        report["stt_config"] = cfg
        snapshot = resolve_snapshot(cfg["model"], cfg["revision"], local_files_only=True)
        report["stt_model"] = snapshot_record(cfg["model"], cfg["revision"], snapshot)
        report["stt_model"]["files_sha256"] = {str(p.relative_to(snapshot)): file_sha256(p)
                                                 for p in sorted(snapshot.rglob("*")) if p.is_file()}
        mlx.reset_peak_memory()
        report["memory_before_load"] = memory(mlx)
        stt = LocalSTT(**cfg)
        stage = "model_load"
        timed(report["model_load_seconds"], "stt", stt.load)
        report["stt_model"]["runtime_loaded_revision"] = snapshot.name
        report["memory_after_load"] = memory(mlx)
        save()
        for trial in trials:
            if trial["status"] != "pending":
                continue  # This row remains in the declared denominator with its failure.
            trial.update(status="running", attempted=True)
            save()
            try:
                stage = "resample"
                source = Path(trial["source"]) / trial["pcm_file"]
                pcm24 = source.read_bytes()
                if not pcm24 or len(pcm24) % 2 or file_sha256(source) != trial["input_sha256"][trial["pcm_file"]]:
                    raise ValueError("PCM is empty, malformed or changed after protocol freeze")
                trial.update(received_samples=len(pcm24) // 2, received_duration_seconds=len(pcm24) / 48000)
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
        report["inputs_unchanged"] = all(file_sha256(Path(t["source"]) / name) == digest
                                          for t in trials for name, digest in t["input_sha256"].items())
        report["source_unchanged"] = all(file_sha256(ROOT / name) == digest for name, digest in frozen["source_sha256"].items())
        report["suite_unchanged"] = all(file_sha256(base / name) == digest for name, digest in frozen["suite_sha256"].items())
    except BaseException as exc:
        report.update(status="error", failed_stage=stage, error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    finally:
        pool = getattr(getattr(stt, "_engine", None), "pool", None)
        if pool is not None:
            try:
                pool.shutdown(wait=True)
            except BaseException as exc:
                report["cleanup_error"] = repr(exc)
                report["status"] = "error"
        for trial in trials:
            if trial["status"] == "pending":
                trial.update(status="not_attempted", reason="setup_or_model_load_failed")
        completed = sum(t["status"] == "completed" for t in trials)
        report["summary"] = {"declared": len(trials), "attempted": sum(t["attempted"] for t in trials),
                             "completed": completed, "failed_or_not_completed": len(trials) - completed,
                             "capture_status_counts": dict(Counter(t.get("capture_status", "missing") for t in trials)),
                             "review_status_counts": dict(Counter(t["status"] for t in trials))}
        if report["status"] == "running":
            verified = all(report.get(key) is True for key in ('inputs_unchanged', 'source_unchanged', 'suite_unchanged'))
            report['status'] = ('completed' if completed == len(trials) and verified and not report.get('cleanup_error')
                                else 'completed_with_errors')
        report["finished_at_utc"] = stamp()
        save()
    print(json.dumps({"status": report["status"], "report": str(output / "report.json"), **report["summary"]}), flush=True)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
