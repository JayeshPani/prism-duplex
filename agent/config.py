"""Load agent/config.yaml + .env.local and build the coordinator stack."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .coordinator.coordinator import CoordinatorConfig
from .coordinator.llm_client import LLMClient, LLMConfig

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.local")


def load_config(profile: str | None = None) -> dict[str, Any]:
    cfg = yaml.safe_load((ROOT / "agent" / "config.yaml").read_text())
    name = profile or os.getenv("PRISM_PROFILE") or cfg["default_profile"]
    prof = cfg["profiles"][name]
    # env overrides (handy for quick experiments without editing the file)
    if os.getenv("PRISM_LLM_MODEL"):
        prof["llm"]["model"] = os.environ["PRISM_LLM_MODEL"]
    if os.getenv("PRISM_LLM_BASE_URL"):
        prof["llm"]["base_url"] = os.environ["PRISM_LLM_BASE_URL"]
    return {"profile": name, "seed": cfg["seed"], "coordinator": cfg["coordinator"], **prof}


def make_llm(cfg: dict[str, Any]) -> LLMClient:
    return LLMClient(LLMConfig(seed=cfg["seed"], **cfg["llm"]))


def make_coordinator_config(cfg: dict[str, Any]) -> CoordinatorConfig:
    return CoordinatorConfig(**cfg["coordinator"])
