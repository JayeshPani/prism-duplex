"""Load agent/config.yaml + .env.local and build the coordinator stack."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .coordinator.coordinator import CoordinatorConfig
from .coordinator.llm_client import LLMClient, LLMConfig
from .model_assets import validate_revision

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.local")


def load_config(profile: str | None = None) -> dict[str, Any]:
    cfg = yaml.safe_load((ROOT / "agent" / "config.yaml").read_text())
    name = profile or os.getenv("PRISM_PROFILE") or cfg["default_profile"]
    prof = cfg["profiles"][name]
    # env overrides (handy for quick experiments without editing the file)
    if os.getenv("PRISM_LLM_MODEL"):
        prof["llm"]["model"] = os.environ["PRISM_LLM_MODEL"]
        # Never inherit the original model's SHA when selecting another model.
        prof["llm"]["revision"] = os.getenv("PRISM_LLM_REVISION")
    elif os.getenv("PRISM_LLM_REVISION"):
        prof["llm"]["revision"] = os.environ["PRISM_LLM_REVISION"]
    if os.getenv("PRISM_LLM_BASE_URL"):
        prof["llm"]["base_url"] = os.environ["PRISM_LLM_BASE_URL"]
    if name in {"local-mac", "local-cuda"}:
        llm = prof["llm"]
        llm["allow_unfrozen"] = os.getenv("PRISM_ALLOW_UNFROZEN_MODELS") == "1" and not llm.get("revision")
        validate_revision(llm["model"], llm.get("revision"), allow_unfrozen=llm["allow_unfrozen"])
    validate_revision(prof["stt"]["model"], prof["stt"].get("revision"))
    return {"profile": name, "seed": cfg["seed"], "coordinator": cfg["coordinator"], **prof}


def make_llm(cfg: dict[str, Any]) -> LLMClient:
    llm = {key: value for key, value in cfg["llm"].items()
           if key not in {"revision", "allow_unfrozen", "request_model"}}
    # MLX's server maps only this alias to its --model snapshot. Sending the
    # repository ID would trigger a second download/load from mutable main.
    llm["model"] = cfg["llm"].get("request_model", llm["model"])
    return LLMClient(LLMConfig(seed=cfg["seed"], **llm))


def make_coordinator_config(cfg: dict[str, Any], *, mode: str | None = None) -> CoordinatorConfig:
    coordinator = dict(cfg["coordinator"])
    if mode == "bench" and "PRISM_BENCH_SPEAK_RESULTS" in os.environ:
        override = os.environ["PRISM_BENCH_SPEAK_RESULTS"]
        if override not in {"0", "1"}:
            raise ValueError("PRISM_BENCH_SPEAK_RESULTS must be 0 or 1")
        coordinator["speak_results"] = override == "1"
    return CoordinatorConfig(**coordinator)


def room_mode(room_name: str) -> str:
    forced = os.getenv("MODE")
    if forced:
        if forced not in {"car", "assistant", "bench"}:
            raise ValueError(f"unknown MODE: {forced}")
        return forced
    if room_name.startswith("car-"):
        return "car"
    return "assistant" if room_name.startswith("web-") else "bench"


def local_turn_handling() -> dict[str, Any]:
    # Explicit in both dev and production: SDK defaults can select cloud models.
    return {"turn_detection": "vad",
            "endpointing": {"min_delay": float(os.getenv("PRISM_MIN_ENDPOINTING", "0.4")), "max_delay": 3.0},
            "interruption": {"enabled": True, "mode": "vad", "resume_false_interruption": True}}
