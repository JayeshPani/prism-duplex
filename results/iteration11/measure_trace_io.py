"""Fixed-order synchronous/owned-writer comparison; injected disk delay, no models."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.coordinator.events import Event
from agent.telemetry.trace_writer import TraceWriter

BASE = Path(__file__).resolve().parent
OUT = BASE / "trace-io-comparison"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SynchronousTrace:
    """Same open/append/flush/close semantics as before/agent/main.py."""
    def __init__(self, path):
        self.path = path

    async def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a")

    def __call__(self, event):
        self.file.write(json.dumps(event.to_dict(), default=str) + "\n")
        self.file.flush()

    async def aclose(self, timeout):
        self.file.close()
        return True


async def main():
    OUT.mkdir(exist_ok=False)
    paths = [Path(__file__), BASE / "before/agent/main.py", ROOT / "agent/main.py",
             ROOT / "agent/telemetry/trace_writer.py", ROOT / "agent/coordinator/events.py"]
    frozen = {str(path.relative_to(ROOT)): digest(path) for path in paths}
    trials = [{"phase": phase, "repetition": repetition, "arm": arm, "status": "pending"}
              for phase in ("open", "flush", "close") for repetition in range(1, 6)
              for arm in ("A", "B")]
    protocol = {"declared_at_utc": datetime.now(timezone.utc).isoformat(), "trials": trials,
                "arms": {"A": "Frozen synchronous trace semantics", "B": "Production TraceWriter"},
                "injected_delay_seconds": .1, "ticker_period_seconds": .001,
                "events_per_trial": 2, "source_sha256": frozen,
                "limits": ["Fixed A/B order and scheduler/background noise; no population latency claim.",
                           "Artificial single disk stall, no RTC or inference. All rows retained.",
                           "Successful close is flush/close acknowledgement, not fsync/crash durability."]}
    (OUT / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    report = {"status": "running", "protocol_sha256": digest(OUT / "protocol.json"), "trials": trials}

    def save():
        (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    original_open = Path.open
    for index, row in enumerate(trials, 1):
        target = OUT / f"{index:02d}-{row['phase']}-{row['arm']}.jsonl"
        delayed = False
        stop = False
        lateness = []

        def delay(phase):
            nonlocal delayed
            if not delayed and row["phase"] == phase:
                delayed = True
                time.sleep(.1)

        class File:
            def __init__(self, file):
                self.file = file

            def write(self, payload):
                return self.file.write(payload)

            def flush(self):
                delay("flush")
                return self.file.flush()

            def close(self):
                delay("close")
                return self.file.close()

        def open_file(path, *args, **kwargs):
            if path == target:
                delay("open")
                return File(original_open(path, *args, **kwargs))
            return original_open(path, *args, **kwargs)

        async def ticker():
            while not stop:
                due = time.perf_counter() + .001
                await asyncio.sleep(.001)
                lateness.append(max(0, time.perf_counter() - due))

        events = [Event("tool_done", {"index": i, "result": {"status": "success"}}, ts=float(i)) for i in range(2)]
        writer = TraceWriter(target) if row["arm"] == "B" else SynchronousTrace(target)
        tick = asyncio.create_task(ticker())
        row["status"] = "running"
        save()
        try:
            await asyncio.sleep(.005)
            began = time.perf_counter()
            with patch.object(Path, "open", open_file):
                await writer.start()
                for event in events:
                    writer(event)
                row["close_complete"] = await writer.aclose(timeout=2)
            row["operation_seconds"] = time.perf_counter() - began
            await asyncio.sleep(.005)
            observed = [json.loads(line) for line in target.read_text().splitlines()]
            row.update(status="completed", delay_injected=delayed,
                       exact_ordered_content=observed == [event.to_dict() for event in events],
                       file_sha256=digest(target))
        except Exception as error:
            row.update(status="error", error=repr(error))
            await writer.aclose(timeout=2)
        finally:
            stop = True
            await tick
            row.update(ticker_samples=len(lateness), ticker_lateness_seconds=lateness,
                       worst_ticker_lateness_seconds=max(lateness, default=None))
            save()
    report.update(status="completed", source_unchanged=all(digest(ROOT / name) == h for name, h in frozen.items()),
                  finished_at_utc=datetime.now(timezone.utc).isoformat())
    save()
    for phase in ("open", "flush", "close"):
        for arm in ("A", "B"):
            group = [row for row in trials if row["phase"] == phase and row["arm"] == arm]
            print(phase, arm, "median worst lateness ms", sorted(row["worst_ticker_lateness_seconds"] * 1000 for row in group)[2],
                  "content", sum(row.get("exact_ordered_content", False) for row in group), "/", len(group))
    assert report["source_unchanged"] and all(row["status"] == "completed" and row["delay_injected"]
        and row["close_complete"] and row["exact_ordered_content"] for row in trials)


if __name__ == "__main__":
    asyncio.run(main())
