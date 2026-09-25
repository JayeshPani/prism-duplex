"""Tool-call telemetry in the exact format FDB-v3 reads.

run_tool_benchmark.py collects /tmp/agent_tool_calls.log lines whose "room"
matches the eval room. Format copied from the reference cascaded_agent.py.
Every executed call is logged - cancelled or blocked calls never execute, so
they never appear here.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

TOOL_LOG = os.getenv("FDB_TOOL_LOG", "/tmp/agent_tool_calls.log")
HEARTBEAT_LOG = os.getenv("FDB_HEARTBEAT_LOG", "/tmp/agent_heartbeat.log")

_lock = threading.Lock()


def log_tool_call(room: str, function: str, args: dict[str, Any], t_start: float, t_end: float) -> None:
    line = json.dumps({
        "room": room,
        "call": {"function": function, "args": args,
                 "timestamp_start": t_start, "timestamp_end": t_end},
    })
    with _lock, open(TOOL_LOG, "a") as f:
        f.write(line + "\n")


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
