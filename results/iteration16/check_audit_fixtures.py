"""Lightweight invented-artifact checks; no models, services or production imports."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("iteration16_audit", HERE / "audit_live.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
PROTOCOL = json.loads((HERE / "protocol.json").read_text())


def fixture(case, mutation=None):
    with tempfile.TemporaryDirectory() as tmp:
        audit.ROOT = Path(tmp)
        audit.BASE = Path(tmp) / "run"
        audit.STACK = audit.BASE / "stack"
        directory = audit.BASE / "fixture"
        directory.mkdir(parents=True)
        (audit.STACK / "traces").mkdir(parents=True)
        (audit.BASE / "audio-assets").mkdir()
        expected = deepcopy(PROTOCOL["expected"][case])
        server, calls, frames, clips, handles = [], [], [], [], []
        def emit(kind, data, at):
            server.append({"type": kind, "data": data, "ts": at + 1000})
        emit("agent_state", {"state": "listening"}, 0)
        emit("session_started", {"room": "fixture"}, .1)
        for v, (declared, effect) in enumerate(zip(expected["turns"], expected["effects"]), 1):
            at = v * 10
            audio = audit.BASE / "audio-assets" / declared["audio"]
            audio.write_bytes(declared["audio"].encode())
            clips.append({"label": "initial" if v == 1 else f"followup_{v-1}", "sha256": audit.digest(audio),
                          "sample_rate": 24000, "samples": 480, "activity_start_sample": 0, "activity_end_sample": 480,
                          "timing": {"started_monotonic": at, "published_samples": 480}})
            emit("user_final", {"intent_version": v, "text": declared["text"]}, at)
            tools = declared["allowed_planned_sequences"][-1]
            planned = [{"id": f"c{i}", "tool": tool, "args": {}} for i, tool in enumerate(tools, 1)]
            emit("plan_ready", {"intent_version": v, "calls": planned, "complete": True}, at + .1)
            emit("gate_committed", {"intent_version": v, "calls": tools}, at + .15)
            rid = f"R{v}"
            for i, tool in enumerate(tools, 1):
                data = {"operation_id": f"turn-{v}", "execution_id": f"exec{v}", "id": f"c{i}", "tool": tool,
                        "args": {"route_id": rid} if tool == "start_navigation" else {}}
                if tool == "search_destination" and v == 3:
                    emit("tool_reused", data, at + i * .2)
                    continue
                emit("tool_started", data, at + i * .2)
                result = {"status": "success"}
                if tool == "compute_route":
                    result["route_id"] = rid
                if tool == effect["tool"]:
                    result.update({k: val for k, val in effect.items() if k != "tool"})
                    if tool == "start_navigation":
                        result["route_id"] = rid
                emit("tool_done", {**data, "result": result, "stale": False}, at + i * .2 + .05)
                calls.append({"room": "fixture", "call": {"function": tool, "args": data["args"],
                    "attempt_id": f"exec{v}:c{i}", "status": "done", "timestamp_start": at + 1000,
                    "timestamp_end": at + 1001}})
            emit("agent_say", {"intent_version": v, "kind": "result", "text": "Fixture summary."}, at + 1)
            handles.append({"intent_version": v, "kind": "result", "queued_at": at + 1,
                            "ended_at": at + 11 if v < len(expected["turns"]) else at + 2,
                            "state": "interruption_requested" if v < len(expected["turns"]) else "finished"})
            frames.append({"pcm_file": "received.pcm", "offset_bytes": (v-1)*960, "size_bytes": 960,
                           "samples": 480, "received_monotonic": at + 1.1, "rms_db": -20})
        if mutation:
            mutation(server, calls, clips, handles)
        report = {"room": "fixture", "status": "completed_observation", "inputs": clips,
                  "tracks": [{"file": "received.pcm"}], "speech_handles": handles, "source_sha256": {},
                  "config": {"threshold_db": -40}}
        events = [{"kind": "coordinator_event", "event": row, "observed_monotonic": row["ts"] - 1000} for row in server]
        for name, data in [("events.jsonl", events), ("frames.jsonl", frames)]:
            (directory / name).write_text("".join(json.dumps(row) + "\n" for row in data))
        (directory / "received.pcm").write_bytes(bytes(960 * len(frames)))
        report["artifact_sha256"] = {name: audit.digest(directory / name) for name in ("events.jsonl", "frames.jsonl", "received.pcm")}
        (directory / "report.json").write_text(json.dumps(report))
        (directory / "worker_manifest.json").write_text(json.dumps({"source_sha256": {}}))
        (audit.STACK / "traces" / "fixture.jsonl").write_text("".join(json.dumps(row) + "\n" for row in server))
        worker = [{"message": f"room cleanup {word}: fixture"} for word in ("started", "finished")]
        return audit.audit_trial({"name": "fixture", "exit_code": 0, "room_departure": {"agent_departed": True}},
                                 case, expected, calls, worker, {})


def main():
    results = []
    for case in ("cancel", "repeat"):
        row = fixture(case)
        assert all(row["checks"].values()), {key: value for key, value in row["checks"].items() if not value}
        assert len(row["turns"]) == len(PROTOCOL["expected"][case]["turns"])
        assert len(row["interruptions"]) == len(PROTOCOL["expected"][case]["required_interruption_pairs"])
        results.append(case + " passes all declared checks")
    def extra_final(server, calls, clips, handles):
        server.append({"type": "user_final", "data": {"intent_version": 4, "text": "Extra."}, "ts": 1040})
    row = fixture("repeat", extra_final)
    assert not row["checks"]["all_final_transcripts_exact"] and all(row["semantic_checks"].values())
    results.append("extra finalized transcript fails independently of correct effects")
    def unsupported_reuse(server, calls, clips, handles):
        for row in server:
            if row["type"] == "tool_reused":
                row["data"]["args"] = {"query": "not previously searched"}
    row = fixture("repeat", unsupported_reuse)
    assert not row["plan_attempt_checks"]["static_search_reuse_provenance"]
    results.append("reused search without matching earlier successful args fails provenance")
    def stale_route(server, calls, clips, handles):
        for row in server:
            if row["data"].get("operation_id") == "turn-3":
                if row["type"] == "tool_done" and row["data"]["tool"] in {"compute_route", "start_navigation"}:
                    row["data"]["result"]["route_id"] = "R1"
                if row["data"]["tool"] == "start_navigation":
                    row["data"]["args"]["route_id"] = "R1"
    row = fixture("repeat", stale_route)
    assert not row["semantic_checks"]["distinct_fresh_route_ids"]
    results.append("third navigation returning an old route ID fails freshness")
    def missing_input(server, calls, clips, handles):
        clips[2].pop("timing")
        handles[1]["state"] = "finished"
    row = fixture("repeat", missing_input)
    assert not row["checks"]["input_published_3"] and not row["checks"]["old_result_interrupted_2_to_3"]
    assert row["interruptions"][1]["handle_interruption_after_input_onset_ms"] is None
    assert row["turns"][2]["speech_end_monotonic"] is None
    results.append("unpublished third input and normally finished prior speech remain failed/null")
    def incomplete_clear(server, calls, clips, handles):
        for row in server:
            if row["type"] == "tool_done" and row["data"]["tool"] == "cancel_navigation":
                row["data"]["result"].pop("destination")
    row = fixture("cancel", incomplete_clear)
    assert not row["semantic_checks"]["effect_state_2"]
    results.append("absent cancellation field is not treated as explicit null")
    print(json.dumps({"status": "passed", "fixture_cases": results,
        "scope": "Invented artifacts only; no model, RTC service or production imports.",
        "audit_source_sha256": hashlib.sha256((HERE / "audit_live.py").read_bytes()).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()
