"""Fresh-server A1/B2/A1 cache-entry experiment; no production settings changed."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def flag(command, name, replacement=None):
    if command.count(name) != 1 or command.index(name) + 1 >= len(command):
        raise ValueError(f"expected exactly one valued {name}")
    index = command.index(name) + 1
    if replacement is None:
        return command[index]
    result = command[:]
    result[index] = str(replacement)
    return result


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_hashes(expected):
    for path, digest in expected.items():
        if sha(path) != digest:
            raise ValueError(f"frozen file changed: {path}")


def require_free(host, port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))  # refuse existing listeners; never terminate them


def stop(process):
    """Signal only the process group created by this harness."""
    if process is None:
        return None
    forced = False
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # exited after poll; still reap the owned child
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forced = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    return {"pid": process.pid, "exit_code": process.returncode, "forced_kill": forced}


def memory(psutil, pid=None):
    row = {"at_utc": datetime.now(timezone.utc).isoformat(), "monotonic": time.monotonic(),
           "system_memory": psutil.virtual_memory()._asdict(),
           "system_swap": psutil.swap_memory()._asdict()}
    if pid is not None:
        try:
            row["server_rss_bytes"] = psutil.Process(pid).memory_info().rss
        except psutil.Error as error:
            row["server_rss_error"] = str(error)
    return row


def sample_memory(psutil, pid, path, stopped):
    with path.open("w") as stream:
        while not stopped.is_set():
            stream.write(json.dumps(memory(psutil, pid)) + "\n")
            stream.flush()
            stopped.wait(0.5)


def run(args):
    import psutil
    from agent.config import load_config

    args.out, args.launch_record = args.out.resolve(), args.launch_record.resolve()
    if args.out.exists():
        raise ValueError("output already exists; choose a new directory")
    launch = json.loads(args.launch_record.read_text())
    command = launch["command"]
    if command[:4] != [sys.executable, "-m", "mlx_lm", "server"]:
        raise ValueError("launch must use this Python interpreter and -m mlx_lm server")
    host, port = flag(command, "--host"), int(flag(command, "--port"))
    if host != "127.0.0.1" or flag(command, "--prompt-cache-size") != "1":
        raise ValueError("expected the recorded loopback, one-entry baseline")
    require_free(host, port)
    cfg = load_config("local-mac")
    if cfg != launch["configuration"]:
        raise ValueError("effective configuration differs from launch record")
    model, llm = launch["model"], cfg["llm"]
    snapshot = Path(flag(command, "--model")).resolve()
    if (not re.fullmatch(r"[0-9a-f]{40}", snapshot.name)
            or snapshot != Path(model["snapshot_path"]).resolve()
            or snapshot.name != llm["revision"] or snapshot.name != model["resolved_revision"]
            or llm["model"] != model["model"] or llm.get("allow_unfrozen")
            or llm.get("request_model") != "default_model"
            or llm["base_url"] != f"http://{host}:{port}/v1"):
        raise ValueError("model selection is not the recorded pinned local snapshot")
    weights = {str(snapshot / name): digest for name, digest in launch["files_sha256"].items()}
    if not any(name.endswith(".safetensors") for name in weights):
        raise ValueError("launch record lacks model weight hashes")
    installed = metadata.distribution("mlx-lm")
    sources = list((ROOT / "agent").rglob("*.py")) + [ROOT / "agent/config.yaml",
        ROOT / "scripts/local_navigation_experiment.py", Path(__file__), args.launch_record,
        installed.locate_file("mlx_lm/server.py"), installed.locate_file("mlx_lm/models/cache.py")]
    sources += [ROOT / ".env.local"] if (ROOT / ".env.local").exists() else []
    frozen = {str(path.resolve()): sha(path) for path in sources}
    agent_paths = set((ROOT / "agent").rglob("*.py"))
    frozen.update(weights)
    verify_hashes(frozen)
    packages = {name: metadata.version(name) for name in launch["packages"]}
    if packages != launch["packages"]:
        raise ValueError("installed model packages differ from launch record")
    args.out.mkdir(parents=True)
    report = {"kind": "cache_entry_count_launch_ablation", "status": "running",
        "created_at": datetime.now(timezone.utc).isoformat(), "configuration": cfg,
        "launch_record": str(args.launch_record.resolve()), "frozen_sha256": frozen,
        "packages": {**packages, "psutil": metadata.version("psutil")},
        "arm_order": ["A1", "B2", "A1_repeat"], "arm_deadline_s": args.arm_timeout,
        "sampling_interval_s": 0.5, "notes": [
            "Only --prompt-cache-size changes; --prompt-cache-bytes is NOT enforced for seeded requests in installed mlx-lm 0.31.3.",
            "Seeded requests use sequential _serve_single; decode/prompt concurrency flags do not parallelize them.",
            "Model-list readiness and local weight selection are not inference evidence. Raw navigation results provide inference observations.",
            "First case of each fresh server is retained as cold; later cases are warm. A/B/A order does not remove all thermal/system-load confounds.",
            "System available RAM is psutil's platform estimate (macOS free+inactive). RSS excludes neither sharing nor GPU overlap; do not add RSS and MLX allocations.",
            "Sampling can miss transient peaks. No audio, held-out benchmark, or cache-hit-token measurement is performed."], "arms": []}
    save = lambda: (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    save()  # Freeze source, weights and protocol before starting any model process.
    env = {**os.environ, "HF_HUB_OFFLINE": "1"}
    try:
        for name, count in [("A1", 1), ("B2", 2), ("A1_repeat", 1)]:
            verify_hashes(frozen)
            if set((ROOT / "agent").rglob("*.py")) != agent_paths:
                raise ValueError("agent source file inventory changed")
            if load_config("local-mac") != cfg:
                raise ValueError("effective configuration changed between arms")
            require_free(host, port)
            folder = args.out / name
            folder.mkdir()
            arm = {"name": name, "status": "running", "command": flag(command, "--prompt-cache-size", count),
                   "memory_before": memory(psutil), "cold_case_repeat": 1}
            report["arms"].append(arm)
            save()
            server = nav = sampler = None
            stopped = threading.Event()
            deadline = time.monotonic() + args.arm_timeout
            try:
                with (folder / "server.log").open("w") as server_log, (folder / "navigation.log").open("w") as nav_log:
                    server = subprocess.Popen(arm["command"], cwd=ROOT, env=env, stdout=server_log,
                                              stderr=subprocess.STDOUT, start_new_session=True)
                    arm["server_pid"] = server.pid
                    sampler = threading.Thread(target=sample_memory, args=(psutil, server.pid, folder / "memory.jsonl", stopped), daemon=True)
                    sampler.start()
                    save()
                    while True:
                        if server.poll() is not None:
                            raise RuntimeError(f"server exited during startup: {server.returncode}")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("arm deadline during startup")
                        try:
                            with urlopen(f"http://{host}:{port}/v1/models", timeout=0.5) as response:
                                arm["readiness_response"] = json.load(response)
                            break
                        except (OSError, ValueError):
                            time.sleep(0.2)
                    listener = subprocess.check_output(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], text=True, timeout=2)
                    if set(listener.split()) != {str(server.pid)}:
                        raise RuntimeError("endpoint listener is not exclusively the owned server")
                    arm["listener_pid_verified"] = server.pid
                    arm["navigation_command"] = [sys.executable, str(ROOT / "scripts/local_navigation_experiment.py"),
                        "--profile", "local-mac", "--case", "return_to_destination", "--repeats", "3", "--out", str(folder / "navigation")]
                    nav = subprocess.Popen(arm["navigation_command"], cwd=ROOT, env=env, stdout=nav_log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                    arm["navigation_pid"] = nav.pid
                    save()
                    nav.wait(timeout=max(0.01, deadline - time.monotonic()))
                    arm["navigation_exit_code"] = nav.returncode
                    arm["status"] = "completed" if nav.returncode == 0 else "navigation_failed"
                    if server.poll() is not None:
                        raise RuntimeError("server exited during navigation")
                    result = json.loads((folder / "navigation/experiment.json").read_text())
                    if result["configuration"] != cfg:
                        raise ValueError("navigation subprocess used a different effective configuration")
                    arm["navigation_summary"] = result["summary"]
            except BaseException as error:
                arm.update(status="failed", error=repr(error))
                raise
            finally:
                for role, process in [("navigation", nav), ("server", server)]:
                    try:
                        arm[role + "_cleanup"] = stop(process)
                    except Exception as error:
                        arm.update(status="cleanup_failed")
                        arm[role + "_cleanup"] = {"error": repr(error)}
                stopped.set()
                if sampler is not None:
                    sampler.join(timeout=2)
                arm["memory_after"] = memory(psutil)
                arm["finished_at"] = datetime.now(timezone.utc).isoformat()
                save()
            verify_hashes(frozen)
            if set((ROOT / "agent").rglob("*.py")) != agent_paths:
                raise ValueError("agent source file inventory changed during arm")
            arm["frozen_files_verified_after_arm"] = True
            save()
        report["status"] = "completed" if all(a["status"] == "completed" for a in report["arms"]) else "completed_with_failures"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    return report["status"] == "completed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch-record", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--arm-timeout", type=float, default=300)
    options = parser.parse_args()
    if options.arm_timeout <= 0:
        parser.error("--arm-timeout must be positive")
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(0 if run(options) else 1)
