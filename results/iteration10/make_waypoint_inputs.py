"""Create two declared synthetic RTC inputs once; retain verbatim ASR checks."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.model_assets import file_sha256
from results.iteration10.measure_number_speech import hashes
from scripts.local_speech_experiment import resample, timed, write_wav

BASE = Path(__file__).resolve().parent
OUT = BASE / "waypoint-input-generation"
OUT.mkdir(exist_ok=False)
os.environ.update(HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1",
                  PRISM_MODEL_RECEIPTS_DIR=str(OUT / "model-receipts"))
method = json.loads((BASE / "number-protocol.json").read_text())
rows = [{"id": name, "text": text, "status": "pending"} for name, text in (
    ("navigate-mgroad", "Navigate to MG Road Metro Station."),
    ("add-coffee", "Add the nearest coffee as a stop."))]
report = {"status": "running", "started_at_utc": datetime.now(timezone.utc).isoformat(),
          "pid": os.getpid(), "command": sys.argv, "rows": rows,
          "script_sha256": file_sha256(Path(__file__)),
          "configuration": method["configuration"],
          "model_reference_sha256": file_sha256(BASE / "number-protocol.json"),
          "limits": "Two first renders, no retries or selection. ASR of whole generated inputs is a preparation check, not RTC recognition or human speech evidence.",
          "cleanup": []}

def save():
    temp = OUT / "report.tmp"
    temp.write_text(json.dumps(report, indent=2) + "\n")
    temp.replace(OUT / "report.json")

def interrupt(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")

save()
signal.signal(signal.SIGTERM, interrupt)
signal.signal(signal.SIGINT, interrupt)
tts = stt = None
try:
    import numpy as np
    from agent.config import load_config
    from agent.pipeline.local_stt import LocalSTT
    from agent.pipeline.local_tts import KokoroTTS
    cfg = load_config("local-mac")
    assert {k: cfg[k] for k in ("stt", "tts")} == method["configuration"]
    report["model_hashes_before"] = hashes(method["model_files_sha256"], Path("/"))
    assert all(x["matches"] for x in report["model_hashes_before"].values())
    tts = KokoroTTS(voice=cfg["tts"]["voice"], speed=cfg["tts"]["speed"])
    stt = LocalSTT(**cfg["stt"])
    timed(report, "tts_load_seconds", tts.load)
    timed(report, "stt_load_seconds", stt.load)
    for row in rows:
        row["status"] = "running"
        save()
        try:
            pcm24 = timed(row, "tts_seconds", tts.render, row["text"])
            wav24 = OUT / (row["id"] + ".wav")
            write_wav(wav24, pcm24, 24000)
            row["wav24"] = {"file": wav24.name, "sha256": file_sha256(wav24), "duration_seconds": len(pcm24) / 48000}
            pcm16 = resample(pcm24)
            wav16 = OUT / (row["id"] + "-16k.wav")
            write_wav(wav16, pcm16, 16000)
            row["wav16"] = {"file": wav16.name, "sha256": file_sha256(wav16)}
            row["transcript"] = timed(row, "stt_seconds", stt.transcribe, np.frombuffer(pcm16, dtype=np.int16))
            row["status"] = "completed"
        except Exception as exc:
            row.update(status="error", error=repr(exc), traceback=traceback.format_exc())
        save()
    report["status"] = "completed" if all(r["status"] == "completed" for r in rows) else "completed_with_errors"
except BaseException as exc:
    report.update(status="error", error=repr(exc), traceback=traceback.format_exc())
finally:
    for row in rows:
        if row["status"] in {"pending", "running"}:
            row["status"] = "not_completed"
    save()
    for name, pool in (("stt", getattr(getattr(stt, "_engine", None), "pool", None)), ("tts", getattr(tts, "_pool", None))):
        if pool is not None:
            pool.shutdown(wait=True)
            report["cleanup"].append({"pool": name, "status": "completed"})
            save()
    report["model_hashes_after"] = hashes(method["model_files_sha256"], Path("/"))
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    save()
print(json.dumps({"status": report["status"], "rows": rows}))
raise SystemExit(0 if report["status"] == "completed" else 1)
