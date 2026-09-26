"""Run the declared probe suite once with an owned loopback stack; retain failures."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import psutil

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent
STACK = BASE / "local-stack-run1"


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    STACK.mkdir(exist_ok=False)
    protocol = json.loads((BASE / "protocol.json").read_text())
    previous = ROOT / "results/iteration4/local-stack-run1"
    (STACK / "livekit.yaml").write_bytes((previous / "livekit.yaml").read_bytes())
    commands = {
        "livekit": ["/opt/homebrew/bin/livekit-server", "--config", str(STACK / "livekit.yaml")],
        "llm": json.loads((previous / "llm-launch.json").read_text())["command"],
        "worker": [sys.executable, "scripts/local_livekit_worker.py", "--manifest", str(STACK / "worker-manifest.json")],
    }
    env = dict(os.environ, LIVEKIT_URL="ws://127.0.0.1:7880", LIVEKIT_API_KEY="devkey", LIVEKIT_API_SECRET="secret",
               PRISM_PROFILE="local-mac", HF_HUB_OFFLINE="1", PRISM_TRACE_DIR=str(STACK / "traces"),
               FDB_TOOL_LOG=str(STACK / "tool-calls.jsonl"), FDB_HEARTBEAT_LOG=str(STACK / "heartbeat.log"),
               PRISM_MODEL_RECEIPTS_DIR=str(STACK / "model-receipts"))
    frozen_paths = [*sorted((ROOT / "agent").rglob("*.py")), ROOT / "agent/config.yaml",
                    ROOT / "scripts/local_livekit_audio_experiment.py", ROOT / "scripts/local_livekit_worker.py", Path(__file__),
                    BASE / "protocol.json", *sorted((BASE / "audio-assets").glob("*.wav"))]
    frozen_hashes = {str(p): sha256(p.read_bytes()).hexdigest() for p in frozen_paths}
    report = {"started_at": stamp(), "status": "starting", "protocol_sha256": sha256((BASE / "protocol.json").read_bytes()).hexdigest(),
              "frozen_sha256": frozen_hashes, "runs": [], "cleanup": [],
              "memory_scope": "System memory/swap plus owned stack parents and descendants; excludes the separate trial client. RSS is not total allocation."}
    processes, logs = {}, []
    active_driver = None
    stop = threading.Event()

    def snapshot_memory():
        with (BASE / "memory.jsonl").open("x") as stream:
            while not stop.is_set():
                row = {"at_utc": stamp(), "monotonic": time.monotonic(), "system_memory": psutil.virtual_memory()._asdict(),
                       "swap": psutil.swap_memory()._asdict(), "owned_processes": {}}
                for role, process in list(processes.items()):
                    try:
                        parent = psutil.Process(process.pid)
                        row["owned_processes"][role] = [{"pid": p.pid, "rss": p.memory_info().rss}
                                                       for p in [parent, *parent.children(recursive=True)]]
                    except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
                        row["owned_processes"][role] = {"error": str(error)}
                stream.write(json.dumps(row) + "\n"); stream.flush()
                stop.wait(.5)

    monitor = threading.Thread(target=snapshot_memory, daemon=True)

    def start(role):
        log = (STACK / f"{role}.log").open("x")
        logs.append(log)
        process = subprocess.Popen(commands[role], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        processes[role] = process
        save(STACK / f"{role}-launch.json", {"pid": process.pid, "command": commands[role], "started_at": stamp()})

    def wait_ready(check, timeout=90):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for role, p in processes.items():
                if p.poll() is not None:
                    raise RuntimeError(f"{role} exited during startup: {p.returncode}")
            try:
                if check():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.5)
        raise TimeoutError("owned stack readiness deadline exceeded")

    try:
        for port, kind in [(7880, socket.SOCK_STREAM), (7882, socket.SOCK_DGRAM), (8081, socket.SOCK_STREAM), (8082, socket.SOCK_STREAM)]:
            with socket.socket(socket.AF_INET, kind) as test:
                test.bind(("127.0.0.1", port))
        monitor.start()
        start("livekit"); start("llm")
        wait_ready(lambda: b"data" in urllib.request.urlopen("http://127.0.0.1:8081/v1/models", timeout=2).read())
        start("worker")
        wait_ready(lambda: "registered worker" in (STACK / "worker.log").read_text())
        report["status"] = "running"
        counts = {}
        for case in protocol["run_order"]:
            if any(p.poll() is not None for p in processes.values()):
                raise RuntimeError("an owned stack process stopped before a trial")
            if any(sha256(Path(p).read_bytes()).hexdigest() != h for p, h in frozen_hashes.items()):
                raise RuntimeError("source, protocol or input changed during the frozen suite")
            counts[case] = counts.get(case, 0) + 1
            name = f"probe-{case}-{counts[case]}"
            cmd = [sys.executable, "scripts/local_livekit_audio_experiment.py", "--input", str(BASE / "audio-assets/navigate.wav"),
                   "--probe", str(BASE / f"audio-assets/{case}.wav"), "--out", str(BASE / name),
                   "--worker-manifest", str(STACK / "worker-manifest.json"), "--protocol", str(BASE / "protocol.json"),
                   "--ready-timeout", "45", "--response-timeout", "60", "--probe-observation-seconds", "12"]
            entry = {"name": name, "case": case, "command": cmd, "started_at": stamp()}
            report["runs"].append(entry)
            save(BASE / "run-report.json", report)
            with (BASE / f"{name}.log").open("x") as log:
                process = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                active_driver = process
                entry["pid"] = process.pid
                try:
                    entry["exit_code"] = process.wait(timeout=150)
                except subprocess.TimeoutExpired:
                    entry["external_deadline_exceeded"] = True
                    process.terminate()
                    try:
                        entry["exit_code"] = process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill(); entry["exit_code"] = process.wait(timeout=5)
                active_driver = None
            entry["finished_at"] = stamp()
            save(BASE / "run-report.json", report)
            print(json.dumps(entry), flush=True)
        report["status"] = "completed" if all(r["exit_code"] == 0 for r in report["runs"]) else "completed_with_failed_captures"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
        stop.set()
        if monitor.is_alive():
            monitor.join(timeout=3)
        cleanup_targets = ([("active_driver", active_driver)] if active_driver is not None else []) + list(reversed(list(processes.items())))
        for role, process in cleanup_targets:
            row = {"role": role, "pid": process.pid, "observed_command": None}
            try:
                if process.poll() is None:
                    try:
                        row["observed_command"] = psutil.Process(process.pid).cmdline()
                    except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
                        row["command_observation_error"] = str(error)
                    process.terminate()
                    row["signal"] = "SIGTERM"
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5); row["escalation"] = "SIGKILL"
            except Exception as error:
                row["cleanup_error"] = repr(error)
            row.update(exit_code=process.returncode, finished_at=stamp())
            report["cleanup"].append(row)
            save(STACK / f"{role}-exit.json", row)
        for log in logs:
            log.close()
        report["frozen_inputs_unchanged"] = all(sha256(Path(p).read_bytes()).hexdigest() == h for p, h in frozen_hashes.items())
        report["finished_at"] = stamp()
        save(BASE / "run-report.json", report)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
