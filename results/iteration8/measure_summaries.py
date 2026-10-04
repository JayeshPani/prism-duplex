"""Measure frozen baseline/direct responders against identical execution inputs."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from agent.config import load_config, make_llm
from agent.coordinator.executor import CallOutcome, Execution, PlannedCall
from agent.coordinator.llm_client import LLMClient
from scripts.cache_policy_experiment import require_free, sample_memory, sha, stop
from scripts.capture_run import validate_frozen_models, validate_local_config


def stamp():
    return datetime.now(timezone.utc).isoformat()


def responder(path, name):
    spec = importlib.util.spec_from_file_location(f"agent.coordinator.{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Responder


class RecordedLLM(LLMClient):
    def __init__(self, base, stream):
        self.cfg, self.client = base.cfg, base.client
        self.stream, self.context, self.count, self.pending = stream, {}, 0, []

    def flush_records(self):
        for row in self.pending:
            self.stream.write(json.dumps(row) + "\n")
        self.stream.flush()
        self.pending.clear()

    async def complete(self, system, user, json_stop=False):
        self.count += 1
        row = {**self.context, "request_number": self.count, "started_at_utc": stamp(),
               "system": system, "user": user, "json_stop": json_stop}
        start = time.monotonic()
        try:
            text = await super().complete(system, user, json_stop)
            row.update(status="completed", response=text)
            return text
        except BaseException as error:
            row.update(status="incomplete", error=repr(error))
            raise
        finally:
            row.update(elapsed_ms=(time.monotonic() - start) * 1000)
            self.pending.append(row)


async def measure(report, save):
    classes = {name: responder(BASE / source, name) for name, source in report["protocol"]["sources"].items()}
    inputs = json.loads((BASE / "inputs.json").read_text())
    with (BASE / "summary-model-requests.jsonl").open("x") as stream:
        client = RecordedLLM(make_llm(report["configuration"]), stream)
        report["llm_configuration"] = asdict(client.cfg)
        try:
            for block, arm in enumerate(report["protocol"]["arm_order"], 1):
                for case in inputs["cases"]:
                    raw = case["execution"]
                    execution = Execution(calls=[PlannedCall(**x) for x in raw["calls"]],
                                          outcomes={k: CallOutcome(**v) for k, v in raw["outcomes"].items()}, stale=raw["stale"])
                    row = {"block": block, "arm": arm, "case": case["id"], "status": "running", "expected_path": case["expected_path"]}
                    report["trials"].append(row)
                    client.context = {k: row[k] for k in ("block", "arm", "case")}
                    save()
                    start, before = time.monotonic(), client.count
                    try:
                        row["text"] = await asyncio.wait_for(classes[arm](client).summarize(case["request"], execution), 40)
                        row["status"] = "completed"
                    except Exception as error:
                        row.update(status="error", error=repr(error))
                    except BaseException as error:
                        row.update(status="interrupted", error=repr(error))
                        raise
                    finally:
                        row.update(elapsed_ms=(time.monotonic() - start) * 1000, logical_model_calls=client.count - before)
                        client.flush_records()
                        save()
        finally:
            await client.client.close()


def main():
    import psutil
    protocol = json.loads((BASE / "summary-protocol.json").read_text())
    for name, expected in protocol["frozen_sha256"].items():
        if sha(ROOT / name) != expected:
            raise ValueError(f"frozen source/input differs: {name}")
    report = {"status": "starting", "started_at_utc": stamp(), "protocol": protocol,
              "protocol_sha256": sha(BASE / "summary-protocol.json"), "trials": []}
    path = BASE / "summary-comparison.json"
    with path.open("x") as stream:
        json.dump(report, stream)

    def save():
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, indent=2) + "\n")
        temp.replace(path)

    process, monitor = None, None
    stopped = threading.Event()
    try:
        command = json.loads((ROOT / "results/iteration7/local-stack-run1/llm-launch.json").read_text())["command"]
        if command[:4] != [sys.executable, "-m", "mlx_lm", "server"]:
            raise ValueError("launch interpreter differs")
        cfg = load_config("local-mac")
        validate_local_config(cfg)
        validate_frozen_models(cfg)
        snapshot = Path(command[command.index("--model") + 1])
        if (cfg != protocol["configuration"] or cfg["llm"]["base_url"] != "http://127.0.0.1:8081/v1"
                or cfg["llm"]["request_model"] != "default_model"
                or snapshot.name != cfg["llm"]["revision"]
                or command[command.index("--host") + 1] != "127.0.0.1"
                or command[command.index("--port") + 1] != "8081"):
            raise ValueError("effective local configuration differs from the frozen launch")
        for name, expected in protocol["model_files_sha256"].items():
            if sha(name) != expected:
                raise ValueError(f"model bytes differ: {name}")
        report["configuration"] = cfg
        require_free("127.0.0.1", 8081)
        with (BASE / "summary-llm.log").open("x") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=dict(os.environ, HF_HUB_OFFLINE="1"),
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            report["launch"] = {"command": command, "pid": process.pid, "started_at_utc": stamp()}
            monitor = threading.Thread(target=sample_memory, args=(psutil, process.pid, BASE / "summary-memory.jsonl", stopped), daemon=True)
            monitor.start()
            save()
            deadline = time.monotonic() + 90
            while True:
                if process.poll() is not None:
                    raise RuntimeError("model server exited before readiness")
                try:
                    if b"data" in urlopen("http://127.0.0.1:8081/v1/models", timeout=2).read():
                        break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("model readiness")
                time.sleep(.5)
            report["status"] = "running"
            asyncio.run(measure(report, save))
            report["status"] = "completed" if all(t["status"] == "completed" for t in report["trials"]) else "completed_with_errors"
    except Exception as error:
        report.update(status="error", error=repr(error))
    except BaseException as error:
        report.update(status="interrupted", error=repr(error))
    finally:
        report["server_exit"] = stop(process)
        stopped.set()
        if monitor:
            monitor.join(timeout=2)
        report.update(finished_at_utc=stamp(), frozen_inputs_unchanged=all(sha(ROOT / p) == h for p, h in protocol["frozen_sha256"].items()))
        report["declared_trials"] = len(protocol["arm_order"]) * protocol["case_count"]
        report["missing_trials"] = report["declared_trials"] - len(report["trials"])
        expected = [(block, arm, case) for block, arm in enumerate(protocol["arm_order"], 1) for case in protocol["case_order"]]
        report["exact_trial_coverage"] = expected == [(t["block"], t["arm"], t["case"]) for t in report["trials"]]
        report["model_bytes_unchanged"] = all(sha(p) == h for p, h in protocol["model_files_sha256"].items())
        if report["status"] == "completed" and (report["missing_trials"] != 0 or not report["exact_trial_coverage"] or not report["frozen_inputs_unchanged"]
                or not report["model_bytes_unchanged"] or report["server_exit"]["forced_kill"]):
            report["status"] = "completed_with_validation_errors"
        save()
    print(json.dumps({k: report[k] for k in ("status", "declared_trials", "missing_trials", "server_exit", "frozen_inputs_unchanged")}))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
