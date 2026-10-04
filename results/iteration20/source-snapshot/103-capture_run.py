"""Capture benchmark provenance without credentials or benchmark answer content."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import distributions
import ipaddress
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.model_assets import PARAKEET_REVISIONS, kokoro_records, validate_revision
from agent.config import make_coordinator_config, room_mode

SAFE_ENV = ("PRISM_PROFILE", "PRISM_LLM_MODEL", "PRISM_ASR", "PRISM_MIN_ENDPOINTING",
            "PRISM_BENCH_SPEAK_RESULTS", "FDB_LATENCY_PROFILE", "LIMIT", "MODE", "JUDGE",
            "JUDGE_MODEL", "PROVIDER", "PRISM_LLM_REVISION", "PRISM_ALLOW_UNFROZEN_MODELS")


def validate_local_config(cfg: dict) -> None:
    """Fail closed on hosted agent configuration; scoring judges are separate."""
    if cfg["profile"] not in {"local-mac", "local-cuda"}:
        raise ValueError("benchmark agent profile must be local-mac or local-cuda")
    url = urlsplit(cfg["llm"]["base_url"])
    host = url.hostname or ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if url.scheme not in {"http", "https"} or not local or url.username or url.password or url.query or url.fragment:
        raise ValueError("benchmark LLM URL must be a loopback HTTP(S) endpoint without credentials")
    if cfg["stt"]["kind"] not in {"parakeet-mlx", "nemo-parakeet"} or cfg["tts"]["kind"] != "kokoro":
        raise ValueError("benchmark speech inference must use local Parakeet and Kokoro")


def validate_frozen_models(cfg: dict) -> None:
    """Development opt-outs must never turn into a purported frozen benchmark."""
    for component in ("llm", "stt"):
        model = cfg[component]
        if model.get("allow_unfrozen"):
            raise ValueError("unfrozen development models cannot be used for a benchmark run")
        validate_revision(model["model"], model.get("revision"))


def command_output(args: list[str], cwd: Path = ROOT) -> str | None:
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
        return p.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_state(root: Path) -> dict:
    patch = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=root, capture_output=True)
    others = command_output(["git", "ls-files", "--others", "--exclude-standard"], root)
    untracked = {}
    for name in (others or "").splitlines():
        if name.startswith(("results/", "bench/data/", "models/")):
            continue  # run artifacts are not source fingerprints
        path = root / name
        if path.is_file():
            untracked[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"commit": command_output(["git", "rev-parse", "HEAD"], root),
            "status": command_output(["git", "status", "--porcelain"], root),
            "tracked_diff_sha256": hashlib.sha256(patch.stdout).hexdigest() if patch.returncode == 0 else None,
            "untracked_source_file_sha256": untracked}


def cached_model(model: str, hub: Path | None = None, *, revision: str | None = None) -> dict:
    hub = hub or Path(os.getenv("HF_HUB_CACHE") or os.getenv("HUGGINGFACE_HUB_CACHE") or
                      str(Path(os.getenv("HF_HOME", str(Path.home() / ".cache/huggingface"))) / "hub"))
    repo = hub / ("models--" + model.replace("/", "--"))
    ref = repo / "refs/main"
    # Retain observed-main inventory for older/unpinned configurations, but a
    # declared revision always takes precedence over the mutable cached ref.
    observed_revision = revision or (ref.read_text().strip() if ref.is_file() else None)
    snapshot = repo / "snapshots" / observed_revision if observed_revision else None
    weights = snapshot is not None and snapshot.is_dir() and any(
        p.is_file() and p.suffix in {".safetensors", ".bin", ".nemo", ".onnx"}
        for p in snapshot.rglob("*"))
    return {"model": model, "declared_revision": revision,
            "cached_revision": observed_revision if weights else None,
            "snapshot_path": str(snapshot) if weights else None,
            "status": "cached_weights_found_not_runtime_verified" if weights else "unresolved",
            "runtime_loaded_revision": None}


def loaded_model_receipts() -> list[dict]:
    directory = os.getenv("PRISM_MODEL_RECEIPTS_DIR")
    if not directory:
        return []
    return [json.loads(path.read_text()) for path in sorted(Path(directory).glob("*.json"))]


def result_inventory(data_dir: Path, benchmark: Path, provider: str) -> dict:
    scenarios = json.loads(benchmark.read_text()).get("scenarios", [])
    known = {s["id"] for s in scenarios}
    inputs = sorted(data_dir.glob("*/input.wav"))
    results = sorted(data_dir.rglob(f"result_{provider}.json"))
    included, excluded, statuses = [], [], {}
    for path in results:
        name = str(path.relative_to(data_dir))
        try:
            data = json.loads(path.read_text())
            if data.get("example_id") not in known:
                excluded.append({"file": name, "reason": "not_in_benchmark_scenario_map"})
                continue
            included.append(name)
            status = str(data.get("status", "unspecified"))
            statuses[status] = statuses.get(status, 0) + 1
        except (ValueError, AttributeError):
            excluded.append({"file": name, "reason": "malformed_result_json"})
    missing = [str(p.parent.relative_to(data_dir)) for p in inputs
               if not (p.parent / f"result_{provider}.json").is_file()]
    return {"expected_inputs": len(inputs), "produced_result_files": len(results),
            "result_file_sha256": {str(p.relative_to(data_dir)): hashlib.sha256(p.read_bytes()).hexdigest() for p in results},
            "evaluator_eligible_files": len(included), "excluded_files": excluded,
            "excluded_count": len(excluded), "missing_results": missing,
            "missing_count": len(missing), "statuses": statuses,
            "eligible_files": included,
            "note": "Eligible means the upstream file-selection rule, not a successful task. Missing files are not scored upstream."}


def asr_mode() -> dict:
    requested = os.getenv("PRISM_ASR", "auto")
    try:
        import torch
        cuda = torch.cuda.is_available()
    except Exception:  # match scripts/fdb_infer.py's CUDA availability detection
        cuda = False
    mlx = requested == "mlx" or (requested == "auto" and not cuda)
    model = "mlx-community/parakeet-tdt-0.6b-v2" if mlx else "nvidia/parakeet-tdt-0.6b-v2"
    return {"requested": requested, "backend": "mlx" if mlx else "nemo-cuda",
            "adapted": mlx, "equivalence_verified": False,
            "model": model, "revision": PARAKEET_REVISIONS[model]}


def capture(cfg: dict, data_dir: Path, provider: str, command: list[str]) -> dict:
    validate_local_config(cfg)
    mode = room_mode("eval-manifest")
    coordinator = asdict(make_coordinator_config(cfg, mode=mode))
    upstream = ROOT / "bench/Full-Duplex-Bench"
    evaluation_asr = asr_mode()
    machine = {"os": platform.platform(), "architecture": platform.machine(),
               "cpu_count": os.cpu_count()}
    if sys.platform == "darwin":
        machine.update(cpu=command_output(["sysctl", "-n", "machdep.cpu.brand_string"]),
                       memory_bytes=command_output(["sysctl", "-n", "hw.memsize"]))
    else:
        machine["cpu"] = platform.processor()
        if Path("/proc/meminfo").exists():
            machine["memory"] = Path("/proc/meminfo").read_text().splitlines()[0]
        machine["gpu"] = command_output(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"])
    models = {name: cached_model(model["model"], revision=model.get("revision")) for name, model in {
        "llm": cfg["llm"], "agent_stt": cfg["stt"], "evaluation_asr": evaluation_asr}.items()}
    models["tts"] = kokoro_records(ROOT / "models/kokoro")
    receipts = loaded_model_receipts()
    for component, model in models.items():
        matching = [r for r in receipts if r.get("component") == component
                    and r.get("model") == model.get("model")
                    and r.get("runtime_loaded_revision") == model.get("declared_revision")
                    and r.get("evidence") == "local_loader_returned"]
        if matching:
            model["runtime_loaded_revision"] = matching[-1]["runtime_loaded_revision"]
            model["status"] = "local_loader_returned_inference_not_attested"
    return {"captured_at_utc": datetime.now(timezone.utc).isoformat(), "command": command,
            "repository": git_state(ROOT), "benchmark": git_state(upstream),
            "configuration": cfg, "environment": {k: os.environ[k] for k in SAFE_ENV if k in os.environ},
            "effective_benchmark": {"mode": mode, "coordinator": coordinator,
                                    "result_speech_silence_ablation": not coordinator["speak_results"]},
            "hardware": machine, "python": sys.version,
            "packages": dict(sorted((d.metadata["Name"], d.version) for d in distributions() if d.metadata["Name"])),
            "models": models, "model_load_receipts": receipts, "evaluation_asr": evaluation_asr,
            "llm_request_model": cfg["llm"].get("request_model", cfg["llm"]["model"]),
            "inference": {"agent_models": "local endpoints only", "audio_transport": "LiveKit; may be cloud",
                          "judge": os.getenv("JUDGE", "none"), "loaded_weights_verified": False},
            "benchmark_timing": "Unmodified upstream input-duration capture and latency definitions",
            "result_inventory": result_inventory(data_dir, upstream / "v3/benchmark_data_v2.json", provider)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate-local-only", action="store_true")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--results-dir", type=Path, default=ROOT / "bench/data/fdb_v3_data_released")
    ap.add_argument("--provider", default="prism")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    from agent.config import load_config
    cfg = load_config()
    validate_local_config(cfg)
    validate_frozen_models(cfg)
    if a.validate_local_only:
        return
    if a.out is None:
        ap.error("--out is required unless --validate-local-only")
    manifest = capture(cfg, a.results_dir, a.provider, a.command[1:] if a.command[:1] == ["--"] else a.command)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
