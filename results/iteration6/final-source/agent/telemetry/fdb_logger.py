"""Tool-call telemetry in the exact format FDB-v3 reads.

run_tool_benchmark.py collects /tmp/agent_tool_calls.log lines whose "room"
matches the eval room. Format copied from the reference cascaded_agent.py.
FDBToolLogger saves a dispatch intention before entering the backend, then
updates that same record when observation ends. An unconfirmed record has no
timestamp_end: the upstream collector subtracts timestamps without accepting
null. Reused or blocked calls never dispatch and have separate trace events.

A process crash can leave an intention without backend entry or acknowledgement;
this telemetry is not proof of exactly-once effects. Even a finalized unknown or
cancelled record's end time is observation end, not confirmed backend completion.
"""

from __future__ import annotations

import json
import os
import threading
import fcntl
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

TOOL_LOG = os.getenv("FDB_TOOL_LOG", "/tmp/agent_tool_calls.log")
HEARTBEAT_LOG = os.getenv("FDB_HEARTBEAT_LOG", "/tmp/agent_heartbeat.log")

_lock = threading.Lock()


def _update_calls(update) -> None:
    """Publish a complete snapshot; readers never see partial JSON or two rows.

    The stable sidecar lock also serializes separate LiveKit worker processes.
    fsync precedes replacement and covers its directory entry. This protects
    process-crash evidence; backend effects still require reconciliation.
    """
    path = Path(TOOL_LOG)
    with _lock, open(str(path) + ".lock", "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
        update(rows)
        temporary_path = None
        try:
            with NamedTemporaryFile("w", dir=path.parent, prefix=path.name + ".", delete=False) as temporary:
                temporary_path = Path(temporary.name)
                for row in rows:
                    temporary.write(json.dumps(row) + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


class FDBToolLogger:
    """One scorer-compatible row per attempt, available while a tool is running."""

    def __init__(self, room: str) -> None:
        self.room = room

    def started(self, attempt_id: str, function: str, args: dict[str, Any], t_start: float) -> None:
        row = {
            "room": self.room,
            "call": {"function": function, "args": args, "timestamp_start": t_start,
                     "attempt_id": attempt_id, "status": "unconfirmed",
                     "dispatch_evidence": "intention_before_backend_entry"},
        }

        def append(rows):
            if any(r.get("room") == self.room and r.get("call", {}).get("attempt_id") == attempt_id for r in rows):
                raise ValueError(f"duplicate attempt identity: {attempt_id}")
            rows.append(row)

        _update_calls(append)

    def finished(self, attempt_id: str, t_end: float, status: str) -> None:
        def finish(rows):
            for row in rows:
                call = row.get("call", {})
                if row.get("room") == self.room and call.get("attempt_id") == attempt_id:
                    call.update(timestamp_end=t_end, status=status,
                                end_evidence="executor_observation_ended")
                    return
            raise ValueError(f"missing dispatch intention: {attempt_id}")

        _update_calls(finish)


def log_tool_call(room: str, function: str, args: dict[str, Any], t_start: float, t_end: float) -> None:
    """Legacy completion-only API. The live agent uses FDBToolLogger instead."""
    row = {
        "room": room,
        "call": {"function": function, "args": args,
                 "timestamp_start": t_start, "timestamp_end": t_end},
    }
    _update_calls(lambda rows: rows.append(row))


def log_latency(room: str, tool: str, user_done_at: float, tool_start_at: float,
                tool_end_at: float, agent_start_at: float) -> None:
    """Same LATENCY_TRACK_JSON line the reference agents write (optional metric)."""
    metrics = {
        "room": room,
        "tool": tool,
        "reasoning": round(tool_start_at - user_done_at, 3) if tool_start_at else 0,
        "execution": round(tool_end_at - tool_start_at, 3) if tool_start_at and tool_end_at else 0,
        "synthesis": round(agent_start_at - (tool_end_at or user_done_at), 3),
        "total": round(agent_start_at - user_done_at, 3),
        "agent_start_at": agent_start_at,
    }
    with _lock, open(HEARTBEAT_LOG, "a") as f:
        f.write(f"LATENCY_TRACK_JSON: {json.dumps(metrics)}\n")
