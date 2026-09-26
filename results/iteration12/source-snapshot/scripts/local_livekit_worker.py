"""Run the existing PRISM agent against local LiveKit; worker health uses port 8082.

Requires LIVEKIT_API_KEY/LIVEKIT_API_SECRET for the local server. The manifest
records launch selection, not proof of model loading, inference or audio delivery.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import distribution, version
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.manifest.exists():
        parser.error("manifest exists; use a new experiment path")
    if os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880") != "ws://127.0.0.1:7880":
        parser.error("this launcher requires LiveKit at ws://127.0.0.1:7880")
    if not all(os.getenv(key) for key in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")):
        parser.error("set the local server credentials in LIVEKIT_API_KEY and LIVEKIT_API_SECRET")
    os.environ["LIVEKIT_URL"] = "ws://127.0.0.1:7880"
    os.environ.setdefault("PRISM_PROFILE", "local-mac")
    os.environ["HF_HUB_OFFLINE"] = "1"

    # Import on the main thread, as required by the installed LiveKit plugins.
    from livekit import agents
    from agent import main as prism

    cfg = prism.CFG
    if (cfg["profile"] != "local-mac" or cfg["llm"].get("allow_unfrozen")
            or urlparse(cfg["llm"]["base_url"]).hostname not in {"127.0.0.1", "localhost", "::1"}):
        parser.error("a frozen local-mac model profile and loopback LLM endpoint are required")
    sdk = distribution("livekit-agents")
    sources = list((ROOT / "agent").rglob("*.py")) + [ROOT / "agent/config.yaml", Path(__file__),
        sdk.locate_file("livekit/agents/worker.py"),
        sdk.locate_file("livekit/agents/ipc/job_thread_executor.py")]
    receipt = {"kind": "local_livekit_worker_launch_selection", "pid": os.getpid(),
        "created_at": datetime.now(timezone.utc).isoformat(), "configuration": cfg,
        "livekit_url": "ws://127.0.0.1:7880", "health_bind": "127.0.0.1:8082",
        "job_executor": "thread", "idle_jobs": 1, "initialize_timeout_s": 120,
        "source_sha256": {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        "packages": {name: version(name) for name in ("livekit-agents", "livekit", "mlx", "mlx-lm", "parakeet-mlx", "kokoro-onnx")},
        "trace_dir": str(prism.TRACE_DIR.resolve()), "tool_log": str(Path(prism.fdb_logger.TOOL_LOG).resolve()),
        "heartbeat_log": str(Path(prism.fdb_logger.HEARTBEAT_LOG).resolve()),
        "notes": ["Calls the existing entrypoint and prewarm; no production agent source changes.",
                  "Manifest precedes prewarm and is not evidence of inference or RTC delivery.",
                  "Thread jobs share process models; native inference is not killed by coroutine cancellation.",
                  "Loopback signaling/health selection does not assert an air-gapped network."]}
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("x") as stream:
        stream.write(json.dumps(receipt, indent=2) + "\n")
    server = agents.AgentServer(job_executor_type=agents.JobExecutorType.THREAD,
        setup_fnc=prism.prewarm, num_idle_processes=1, initialize_process_timeout=120,
        host="127.0.0.1", port=8082, ws_url="ws://127.0.0.1:7880")
    server.rtc_session(prism.entrypoint)
    sys.argv = [sys.argv[0], "start"]  # no development reloader or console audio
    agents.cli.run_app(server)


if __name__ == "__main__":
    main()
