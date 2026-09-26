"""Declared slow-snapshot experiment; fresh subprocesses, no models or audio."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import platform
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def trial(source, phase, directory):
    from agent.coordinator.events import EventBus
    from agent.coordinator.ledger import Ledger
    from agent.telemetry import fdb_logger
    from agent.tools.manifest import Manifest, ToolSpec

    spec = importlib.util.spec_from_file_location("agent.coordinator.measured_executor", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    fdb_logger.TOOL_LOG = str(directory / "tool-calls.jsonl")
    real_replace = fdb_logger.os.replace
    replacements, injected, effects, lateness = [], [], [], []
    loop = asyncio.get_running_loop()
    stop = False

    def slow_replace(*args):
        replacements.append(time.monotonic())
        if len(replacements) == (1 if phase == "start" else 2):
            start = time.monotonic()
            time.sleep(.1)
            injected.append(time.monotonic() - start)
        return real_replace(*args)

    async def ticker():
        while not stop:
            deadline = loop.time() + .005
            await asyncio.sleep(.005)
            lateness.append(loop.time() - deadline)

    async def act():
        effects.append(time.monotonic())
        rows = [json.loads(line) for line in Path(fdb_logger.TOOL_LOG).read_text().splitlines()]
        assert len(rows) == 1 and rows[0]["call"]["status"] == "unconfirmed"
        return {"status": "success"}

    manifest = Manifest("slow-snapshot")
    manifest.add(ToolSpec("act", "", {}, [], True, act))
    engine = module.Executor(manifest, Ledger(), EventBus(),
                             attempt_logger=fdb_logger.FDBToolLogger("measured-room"))
    fdb_logger.os.replace = slow_replace
    task = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(.02)
        start = loop.time()
        execution = await engine.run([module.PlannedCall("c1", "act", {})])
        elapsed = loop.time() - start
        drained = await engine.aclose(timeout=1)
        await asyncio.sleep(.02)
    finally:
        stop = True
        await task
        fdb_logger.os.replace = real_replace
    rows = [json.loads(line) for line in Path(fdb_logger.TOOL_LOG).read_text().splitlines()]
    return {"phase": phase, "max_loop_lateness_seconds": max(lateness),
            "loop_lateness_seconds": lateness, "injected_sleep_seconds": injected,
            "effects": len(effects), "rows": rows, "drained": drained,
            "outcome": execution.outcomes["c1"].status, "run_seconds": elapsed,
            "source_sha256": digest(source), "helper_sha256": digest(Path(__file__)),
            "python": sys.version, "platform": platform.platform()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executor-source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--trial-phase", choices=["start", "finish"])
    args = parser.parse_args()
    args.executor_source = args.executor_source.resolve()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    if args.trial_phase:
        result = asyncio.run(trial(args.executor_source, args.trial_phase, args.out))
        (args.out / "report.json").write_text(json.dumps(result, indent=2) + "\n")
        return
    protocol = json.loads((Path(__file__).parent / "protocol.json").read_text())
    rows = []
    for index, phase in enumerate(protocol["planned_comparison"]["run_order"]):
        directory = args.out / f"{index + 1:02d}-{phase}"
        command = [sys.executable, str(Path(__file__).resolve()), "--executor-source",
                   str(args.executor_source), "--out", str(directory), "--trial-phase", phase]
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
        row = {"phase": phase, "command": command, "exit_code": run.returncode,
               "stdout": run.stdout, "stderr": run.stderr}
        if (directory / "report.json").exists():
            row["result"] = json.loads((directory / "report.json").read_text())
        rows.append(row)
    result = {"protocol_sha256": digest(Path(__file__).parent / "protocol.json"),
              "source_sha256": digest(args.executor_source),
              "helper_sha256": digest(Path(__file__)), "runs": rows}
    (args.out / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"trials": len(rows), "exit_codes": [r["exit_code"] for r in rows]}))


if __name__ == "__main__":
    main()
