"""Exercise declared acoustic cases with a transparent baseline or recognition-aware output."""
import asyncio
import argparse
import signal
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
METHODS = Path(__file__).resolve().parent
BASE = None
STACK = None


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


async def observe_departure(room, seconds):
    from livekit import api, rtc

    result = {"room": room, "started_at": stamp(), "observations": [], "agent_departed": False}
    began = time.monotonic()
    async with api.LiveKitAPI(url="http://127.0.0.1:7880", api_key="devkey", api_secret="secret", failover=False) as client:
        try:
            async with asyncio.timeout(seconds):
                while True:
                    row = {"at": stamp(), "elapsed_seconds": time.monotonic() - began}
                    try:
                        response = await client.room.list_participants(api.ListParticipantsRequest(room=room))
                        row["participants"] = [{"identity": p.identity, "kind": p.kind} for p in response.participants]
                        absent = not any(p.kind == int(rtc.ParticipantKind.PARTICIPANT_KIND_AGENT) for p in response.participants)
                    except Exception as error:
                        row["error"] = repr(error)
                        absent = getattr(error, "code", None) == "not_found"
                    result["observations"].append(row)
                    if absent:
                        result["agent_departed"] = True
                        break
                    await asyncio.sleep(.5)
        except TimeoutError:
            result["deadline_exceeded"] = True
    result.update(finished_at=stamp(), elapsed_seconds=time.monotonic() - began)
    return result


def main():
    global BASE, STACK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', choices=('baseline', 'candidate'), required=True)
    args = parser.parse_args()
    BASE = ROOT / 'results' / f'iteration18-{args.arm}'
    STACK = BASE / 'local-stack-run1'
    from common import verify_frozen, digest
    preflight = verify_frozen()
    preflight_path = METHODS / 'preflight.json'
    global_protocol = json.loads((METHODS / 'protocol.json').read_text())
    protocol = json.loads((BASE / 'protocol.json').read_text())
    plan = json.loads((BASE / 'plan.json').read_text())
    assert protocol['run_order'] == next(x['cases'] for x in global_protocol['arms'] if x['id'] == args.arm)
    assets = json.loads((METHODS / 'input-assets.json').read_text())
    assert all(digest(METHODS / 'audio-assets' / name) == row['sha256'] for name,row in assets['assets'].items())
    baseline_launch = ROOT / 'results/iteration16/local-stack-run1/llm-launch.json'
    assert digest(baseline_launch) == plan['baseline_llm_launch_sha256']
    assert plan['llm_command'] == json.loads(baseline_launch.read_text())['command']
    assert plan['llm_command'][plan['llm_command'].index('--prompt-cache-size')+1] == '2'
    baseline_livekit = ROOT / 'results/iteration16/local-stack-run1/livekit.yaml'
    livekit_config = baseline_livekit.read_bytes()
    assert sha256(livekit_config).hexdigest() == plan['baseline_livekit_config_sha256']
    STACK.mkdir(exist_ok=False)
    (STACK / 'conditions').mkdir()
    (STACK / 'livekit.yaml').write_bytes(livekit_config)
    condition_path = STACK / 'active-condition.json'
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    commands = {
        "livekit": ["/opt/homebrew/bin/livekit-server", "--config", str(STACK / "livekit.yaml")],
        "llm": list(plan["llm_command"]),
        "worker": [sys.executable, str(METHODS / "controlled_worker.py"), "--manifest", str(STACK / "worker-manifest.json"), "--policy", args.arm, "--condition-file", str(condition_path), "--protocol", str(METHODS / "protocol.json")],
    }
    env = dict(os.environ, LIVEKIT_URL="ws://127.0.0.1:7880", LIVEKIT_API_KEY="devkey", LIVEKIT_API_SECRET="secret",
               PRISM_PROFILE="local-mac", HF_HUB_OFFLINE="1", PRISM_TRACE_DIR=str(STACK / "traces"),
               FDB_TOOL_LOG=str(STACK / "tool-calls.jsonl"), FDB_HEARTBEAT_LOG=str(STACK / "heartbeat.log"),
               PRISM_MODEL_RECEIPTS_DIR=str(STACK / "model-receipts"))
    frozen_paths = [Path(name) for name in preflight['source_sha256']]
    frozen_paths += [preflight_path, METHODS / 'input-assets.json', *sorted((METHODS / 'audio-assets').glob('*.wav'))]
    frozen_hashes = {str(p): sha256(p.read_bytes()).hexdigest() for p in frozen_paths}
    report = {"started_at": stamp(), "status": "starting", "arm_id": protocol["arm_id"],
              "cache_entries": protocol["cache_entries"], "plan_sha256": sha256((BASE / "plan.json").read_bytes()).hexdigest(), "protocol_sha256": sha256((BASE / "protocol.json").read_bytes()).hexdigest(),
              "preflight_sha256": sha256(preflight_path.read_bytes()).hexdigest(),
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
            name = f"rtc-{case}-{counts[case]}"
            expected = next(x for x in global_protocol["cases"] if x["id"] == case)
            condition = {"case_id":case, "delay_seconds":expected.get("recognition_delivery_delay_seconds",0)}
            save(STACK / "conditions" / f"{name}.json", condition)
            save(condition_path, condition)
            cmd = [sys.executable, "scripts/local_livekit_audio_experiment.py", "--input", str(METHODS / "audio-assets" / expected["inputs"][0]["asset"]),
                   "--out", str(BASE / name),
                   "--worker-manifest", str(STACK / "worker-manifest.json"), "--protocol", str(BASE / "protocol.json"),
                   "--ready-timeout", "45", "--response-timeout", "60", "--probe-observation-seconds", "30"]
            if len(expected['inputs']) > 1:
                if case == 'ambiguous_road_then_hospital':
                    cmd += ['--followup', str(METHODS / 'audio-assets' / expected['inputs'][1]['asset']),
                            '--followup-trigger', 'response-finished']
                else:
                    cmd += ['--probe', str(METHODS / 'audio-assets' / expected['inputs'][1]['asset'])]
            entry = {"name": name, "case": case, "command": cmd, "started_at": stamp(), "condition": condition, "condition_sha256": digest(condition_path)}
            report["runs"].append(entry)
            save(BASE / "run-report.json", report)
            with (BASE / f"{name}.log").open("x") as log:
                process = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                active_driver = process
                entry["pid"] = process.pid
                try:
                    entry["exit_code"] = process.wait(timeout=protocol["protocol_limits"]["trial_process_deadline_seconds"])
                except subprocess.TimeoutExpired:
                    entry["external_deadline_exceeded"] = True
                    process.terminate()
                    try:
                        entry["exit_code"] = process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill(); entry["exit_code"] = process.wait(timeout=5)
                active_driver = None
            entry["finished_at"] = stamp()
            capture_path = BASE / name / "report.json"
            if capture_path.exists():
                capture = json.loads(capture_path.read_text())
                entry["capture_status"] = capture["status"]
                entry["capture_cleanup_errors"] = capture.get("cleanup_errors", [])
                entry["room_departure"] = asyncio.run(observe_departure(
                    capture["room"], protocol["protocol_limits"]["agent_departure_observation_seconds"]))
            save(BASE / "run-report.json", report)
            print(json.dumps(entry), flush=True)
            if not entry.get('room_departure', {}).get('agent_departed'):
                raise RuntimeError('Previous room departure unverified; stop before rebinding the condition file')
        report["status"] = "completed" if all(r["exit_code"] == 0 for r in report["runs"]) else "completed_with_failed_captures"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
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
        stop.set()
        if monitor.is_alive():
            monitor.join(timeout=3)
        for log in logs:
            log.close()
        try:
            report['frozen_inputs_unchanged'] = all(sha256(Path(p).read_bytes()).hexdigest() == h for p,h in frozen_hashes.items())
        except Exception as error:
            report['frozen_inputs_unchanged'] = False
            report['final_freeze_error'] = repr(error)
        report["all_agents_departed"] = len(report["runs"]) == len(protocol["run_order"]) and all(
            row.get("room_departure", {}).get("agent_departed") for row in report["runs"])
        report["worker_clean_exit"] = any(row["role"] == "worker" and row["exit_code"] == 0
                                          and not row.get("escalation") for row in report["cleanup"])
        if report["status"] == "completed" and (not report["frozen_inputs_unchanged"] or not report["all_agents_departed"] or not report["worker_clean_exit"]
                                                or any(row.get("capture_cleanup_errors") for row in report["runs"])):
            report["status"] = "completed_with_cleanup_failures"
        report["finished_at"] = stamp()
        save(BASE / "run-report.json", report)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
