#!/usr/bin/env python3
"""Local capacity probe: concurrent independent STT, planner, and TTS workloads.

One shared production STT queue and one Kokoro queue serve 1, 2, then 4 rooms.
This is not a causal conversation, audio transport, or acoustic latency test.
Only already cached, frozen local model assets are allowed; nothing downloads.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import platform
import re
import resource
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LEVELS = (1, 2, 4)
PLANNER_TEXT = "Navigate to the office."
TTS_TEXT = "Your route is ready."


def plan_matches(plan):
    """Check office destination and dependency wiring; never dispatch the plan."""
    calls = plan.calls
    names = [call.tool for call in calls]
    if not plan.complete or len({call.id for call in calls}) != len(calls):
        return False
    office = lambda text: isinstance(text, str) and " ".join(re.findall(r"\w+", text.lower())) in {
        "office", "work", "p_office", "office manyata tech park", "manyata tech park"}
    if names == ["compute_route", "start_navigation"]:
        destination_ok = office(calls[0].args.get("destination"))
    elif names == ["search_destination", "compute_route", "start_navigation"]:
        destination_ok = (set(calls[0].args) == {"query"} and office(calls[0].args.get("query"))
                          and calls[1].args.get("destination") == f"${calls[0].id}.places[0].place_id")
    else:
        return False
    route, start = calls[-2:]
    return (destination_ok and not set(route.args) - {"destination", "via"}
            and route.args.get("via") in (None, "")
            and start.args == {"route_id": f"${route.id}.route_id"})


async def experiment(args):
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "raw").mkdir()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["PRISM_MODEL_RECEIPTS_DIR"] = str(output / "model_receipts")
    report = {"status": "preflight", "started_at": datetime.now(timezone.utc).isoformat(), "command": sys.argv,
              "scope": "Concurrent independent local component capacity workloads; no causal conversation or tool execution",
              "expected": {"levels": LEVELS, "stt_reference": args.reference, "planner_input": PLANNER_TEXT,
                           "planner_contract": "complete office route+start plan, optional preceding office lookup; string place arguments, no extra argument keys/calls/waypoint",
                           "tts_input": TTS_TEXT, "tts_check": "nonempty PCM only, speech content not scored"},
              "batch_deadline_s": args.batch_timeout,
              "memory_sample_interval_s": 0.25,
              "limitations": ["Repeated synthetic audio and one planner request; no general conversational accuracy claim",
                              "Same warm process/server, ascending 1/2/4 order, first cold inference retained; caches/order confound scaling",
                              "Component wall time includes production queue waiting; no audible or network transport latency",
                              "Deadline bounds observation, not native execution; timed-out native threads may delay process exit",
                              "RSS and MLX allocations overlap and must not be added; server peak is sampled, not lifetime peak",
                              "Configured local endpoint and supplied PID are not proof they refer to the same server"],
              "batches": [{"rooms": n, "status": "pending", "attempts": [
                  {"room": room, "component": component, "status": "pending"}
                  for room in range(1, n + 1) for component in ("stt", "llm", "tts")]} for n in LEVELS]}

    def save():
        temporary = output / "report.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "report.json")

    save()
    base = stt = tts = mlx = None
    try:
        import numpy as np
        import psutil
        from agent.config import load_config, make_llm
        from agent.coordinator.resolver import IntentResolver
        from agent.model_assets import file_sha256, kokoro_records, resolve_snapshot, snapshot_record
        from agent.pipeline.local_stt import LocalSTT
        from agent.pipeline.local_tts import KokoroTTS, MODELS_DIR
        from agent.tools.car_tools import build_car_manifest
        from scripts.capture_run import git_state, validate_frozen_models, validate_local_config
        from scripts.local_navigation_experiment import RecordedLLM
        from scripts.local_speech_experiment import score, write_wav

        cfg = load_config(args.profile)
        validate_local_config(cfg)
        validate_frozen_models(cfg)
        report.update(configuration=cfg, repository=git_state(ROOT), harness_sha256=file_sha256(Path(__file__)),
                      source_sha256={str(path.relative_to(ROOT)): file_sha256(path)
                                     for path in sorted((ROOT / "agent").rglob("*.py"))},
                      hardware={"platform": platform.platform(), "python": sys.version, "cpu_count": os.cpu_count(),
                                "physical_memory_bytes": psutil.virtual_memory().total},
                      packages=dict(sorted((d.metadata["Name"], d.version) for d in distributions() if d.metadata["Name"])), models={})
        report["source_sha256"]["agent/config.yaml"] = file_sha256(ROOT / "agent/config.yaml")
        save()
        for component in ("stt", "llm"):
            model = cfg[component]
            snapshot = resolve_snapshot(model["model"], model["revision"], local_files_only=True)
            if not any(p.is_file() and p.stat().st_size for suffix in ("*.safetensors", "*.nemo", "*.bin") for p in snapshot.glob(suffix)):
                raise FileNotFoundError(f"{component} weights missing from frozen snapshot")
            for index in snapshot.glob("*.index.json"):
                for weight in set(json.loads(index.read_text()).get("weight_map", {}).values()):
                    if not (snapshot / weight).is_file():
                        raise FileNotFoundError(f"{component} missing weight shard {weight}")
            report["models"][component] = snapshot_record(model["model"], model["revision"], snapshot)
        report["tts_assets"] = kokoro_records(MODELS_DIR)
        if any(not asset["observed_sha256"] for asset in report["tts_assets"].values()):
            raise FileNotFoundError("Kokoro model or voice assets missing")
        with wave.open(str(args.audio), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
                raise ValueError("--audio must be mono 16 kHz 16-bit PCM WAV")
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        if not pcm.size:
            raise ValueError("--audio is empty")
        report["input_audio"] = {"path": str(args.audio.resolve()), "sha256": file_sha256(args.audio), "seconds": pcm.size / 16000}
        process, server = psutil.Process(), psutil.Process(args.llm_pid) if args.llm_pid else None
        report["external_server"] = {"pid": server.pid, "command": server.cmdline(), "created": server.create_time()} if server else None
        if cfg["stt"]["kind"] == "parakeet-mlx":
            import mlx.core as mlx
            mlx.reset_peak_memory()

        def memory():
            server_error = None
            try:
                server_rss = server.memory_info().rss if server else None
            except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
                server_rss, server_error = None, repr(error)
            return {"at_monotonic": time.monotonic(), "process_rss_bytes": process.memory_info().rss,
                    "process_lifetime_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                    "mlx_active_bytes": mlx.get_active_memory() if mlx else None,
                    "mlx_peak_bytes": mlx.get_peak_memory() if mlx else None,
                    "mlx_cache_bytes": mlx.get_cache_memory() if mlx else None,
                    "external_server_rss_bytes": server_rss, "external_server_error": server_error}

        report["memory_before_load"] = memory()
        stt, tts = LocalSTT(**cfg["stt"]), KokoroTTS(voice=cfg["tts"]["voice"], speed=cfg["tts"]["speed"])
        report["load_seconds"] = {}
        for component, loader in (("stt", stt.load), ("tts", tts.load)):
            started = time.monotonic()
            await asyncio.to_thread(loader)
            report["load_seconds"][component] = time.monotonic() - started
        report["memory_after_load"] = memory()
        report["model_load_receipts"] = [json.loads(p.read_text()) for p in sorted((output / "model_receipts").glob("*.json"))]
        for asset in report["tts_assets"].values():
            asset["runtime_loaded"] = True
        base = make_llm(cfg)
        report["status"] = "running"
        save()
        for batch in report["batches"]:
            batch.update(status="running", memory_samples=[memory()])
            started = time.monotonic()
            with (output / "raw" / f"rooms-{batch['rooms']}-llm.jsonl").open("w") as trace:
                async def one(attempt):
                    room, component = attempt["room"], attempt["component"]
                    attempt.update(status="running", started_monotonic=time.monotonic())
                    save()
                    try:
                        if component == "stt":
                            text = await stt._engine.transcribe(pcm)
                            attempt["queue_inclusive_seconds"] = time.monotonic() - attempt["started_monotonic"]
                            attempt.update(transcript=text, **score(args.reference, text))
                            attempt["passed"] = attempt["exact_normalized"]
                        elif component == "tts":
                            audio = await asyncio.get_running_loop().run_in_executor(tts._pool, tts.render, TTS_TEXT)
                            attempt["queue_inclusive_seconds"] = time.monotonic() - attempt["started_monotonic"]
                            path = output / "raw" / f"rooms-{batch['rooms']}-room-{room}.wav"
                            write_wav(path, audio, 24000)
                            attempt.update(audio_file=str(path.relative_to(output)), audio_sha256=file_sha256(path),
                                           audio_seconds=len(audio) / 48000, passed=bool(audio))
                        else:
                            def record(kind, **data):
                                trace.write(json.dumps({"room": room, "kind": kind, **data}) + "\n")
                                trace.flush()
                            resolver = IntentResolver(RecordedLLM(base, record), build_car_manifest())
                            plan = await resolver.resolve(PLANNER_TEXT, [], {}, [])
                            attempt["queue_inclusive_seconds"] = time.monotonic() - attempt["started_monotonic"]
                            attempt.update(plan=asdict(plan), passed=plan_matches(plan))
                        attempt["status"] = "completed"
                    except BaseException as error:
                        attempt.update(status="observation_cancelled_native_may_continue" if isinstance(error, asyncio.CancelledError) else "error",
                                       error=repr(error), passed=False, queue_inclusive_seconds=time.monotonic() - attempt["started_monotonic"])
                        if isinstance(error, asyncio.CancelledError):
                            raise
                    finally:
                        save()

                async def sample():
                    while True:
                        await asyncio.sleep(0.25)
                        batch["memory_samples"].append(memory())

                sampler = asyncio.create_task(sample())
                try:
                    await asyncio.wait_for(asyncio.gather(*(one(a) for a in batch["attempts"])), timeout=args.batch_timeout)
                    batch["status"] = "completed"
                except TimeoutError:
                    batch["status"] = "deadline_exceeded_native_may_continue"
                finally:
                    sampler.cancel()
                    await asyncio.gather(sampler, return_exceptions=True)
                    batch.update(total_wall_seconds=time.monotonic() - started, memory_after=memory())
                    batch["memory_samples"].append(batch["memory_after"])
                    batch["sampled_peak_process_rss_bytes"] = max(s["process_rss_bytes"] for s in batch["memory_samples"])
                    values = [s["external_server_rss_bytes"] for s in batch["memory_samples"] if s["external_server_rss_bytes"] is not None]
                    batch["sampled_peak_server_rss_bytes"] = max(values) if values else None
                    save()
            if batch["status"] != "completed":
                break
        report["status"] = "completed" if all(b["status"] == "completed" for b in report["batches"]) else "incomplete"
    except Exception as error:
        report.update(status="error", error=repr(error))
    finally:
        if base:
            await base.client.close()
        for pool in ([stt._engine.pool] if stt else []) + ([tts._pool] if tts else []):
            pool.shutdown(wait=False, cancel_futures=True)
        attempts = [a for b in report["batches"] for a in b["attempts"]]
        report["summary"] = {"planned": len(attempts), "completed": sum(a["status"] == "completed" for a in attempts),
                             "passed_declared_checks": sum(a.get("passed", False) for a in attempts)}
        report["finished_observing_at"] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({"report": str(output / "report.json"), "status": report["status"], **report["summary"]}), flush=True)
    return report["status"] == "completed" and report["summary"]["passed_declared_checks"] == report["summary"]["planned"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--reference", default="Navigate to Maple Library.")
    parser.add_argument("--profile", choices=("local-mac", "local-cuda"), default="local-mac")
    parser.add_argument("--llm-pid", type=int)
    parser.add_argument("--batch-timeout", type=float, default=180)
    args = parser.parse_args()
    if args.batch_timeout <= 0 or not re.search(r"[a-z0-9]", args.reference.lower()):
        parser.error("batch-timeout and a nonempty reference are required")
    sys.exit(0 if asyncio.run(experiment(args)) else 1)
