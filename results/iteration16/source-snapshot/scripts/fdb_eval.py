"""Run the FDB-v3 evaluators (unmodified) with a configurable judge.

    python scripts/fdb_eval.py --provider prism --results-dir bench/data/fdb_v3_data_released --out results/run_x

Judge selection (JUDGE env):
  none    upstream exact-match fallback, no response judge
  openai  upstream GPT-4o judge (needs OPENAI_API_KEY)
  local   any OpenAI-compatible server (JUDGE_BASE_URL, JUDGE_MODEL), e.g. the local Qwen
  kimi    Moonshot API (MOONSHOT_API_KEY, JUDGE_MODEL)

Alternative judges modify the model, temperature and token budget; report them
separately. Scoring code and item outputs remain untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V3 = ROOT / "bench" / "Full-Duplex-Bench" / "v3"
sys.path.insert(0, str(V3))
sys.path.insert(0, str(ROOT))

from scripts.capture_run import result_inventory


class _ModelSwap:
    """Wraps an OpenAI client so the evaluators' hard-coded model='gpt-4o' goes to another model."""

    def __init__(self, client, model: str, extra: dict):
        self._c, self._m, self._x = client, model, extra
        self.chat = self
        self.completions = self

    def create(self, **kw):
        kw["model"] = self._m
        kw.update(self._x)
        kw["max_tokens"] = max(kw.get("max_tokens", 200), 800)
        r = self._c.chat.completions.create(**kw)
        import re
        msg = r.choices[0].message
        msg.content = re.sub(r"<think>.*?</think>", "", msg.content or "", flags=re.S).strip()
        return r

    def close(self):
        self._c.close()


class _ObservedJudge:
    """Record model identity and request failures without saving credentials/prompts."""
    def __init__(self, client, metadata: dict):
        self.client, self.metadata = client, metadata
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.metadata["requests"] += 1
        try:
            response = self.client.chat.completions.create(**kw)
        except Exception:
            self.metadata["request_errors"] += 1
            raise
        model = getattr(response, "model", None)
        if model and model not in self.metadata["returned_models"]:
            self.metadata["returned_models"].append(model)
        return response


def _judge_client():
    from dotenv import load_dotenv
    from openai import OpenAI
    load_dotenv(ROOT / ".env.local")
    judge = os.getenv("JUDGE", "none")
    if judge == "openai":
        return OpenAI()
    if judge == "local":
        c = OpenAI(base_url=os.getenv("JUDGE_BASE_URL", "http://127.0.0.1:8081/v1"), api_key="local")
        return _ModelSwap(c, os.getenv("JUDGE_MODEL", "mlx-community/Qwen3-14B-4bit"),
                          {"temperature": 0})
    if judge == "kimi":
        c = OpenAI(base_url="https://api.moonshot.ai/v1", api_key=os.environ["MOONSHOT_API_KEY"], max_retries=8)
        return _ModelSwap(c, os.getenv("JUDGE_MODEL", "kimi-k2.6"), {"temperature": 1})
    return None


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.local")
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="prism")
    ap.add_argument("--results-dir", default=str(ROOT / "bench" / "data" / "fdb_v3_data_released"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    judge = os.getenv("JUDGE", "none")
    if judge not in {"none", "openai", "local", "kimi"}:
        ap.error("JUDGE must be none, openai, local or kimi")
    use_llm = judge != "none"
    requested_model = {"none": None, "openai": "gpt-4o", "local": "mlx-community/Qwen3-14B-4bit",
                       "kimi": "kimi-k2.6"}[judge]
    if judge in {"local", "kimi"}:
        requested_model = os.getenv("JUDGE_MODEL", requested_model)
    metadata = {"judge": judge, "model": requested_model, "requested_model": requested_model, "returned_models": [],
                "requests": 0, "request_errors": 0, "evaluation_complete": False,
                "adapted_judge": judge in {"local", "kimi"},
                "fallback_note": "Upstream silently falls back to exact matching on judge/parse errors; request_errors does not count parse fallbacks."}
    invocation = list(sys.argv)
    bench = str(V3 / "benchmark_data_v2.json")
    inventory = result_inventory(Path(a.results_dir), Path(bench), a.provider)
    inventory.update(command=invocation, evaluated_files=None)
    (out / "evaluation_inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    if inventory["evaluator_eligible_files"] == 0:
        ap.error("no eligible result files; refusing to report an empty evaluation")
    if any(x["reason"] == "malformed_result_json" for x in inventory["excluded_files"]):
        ap.error("malformed result JSON; see evaluation_inventory.json")

    import evaluate_pass_rate as epr  # type: ignore
    import evaluate_tool_calls as etc  # type: ignore
    if use_llm:
        client = _ObservedJudge(_judge_client(), metadata)
        epr._openai_client = client
        etc._openai_client = client

    common = ["--benchmark", bench, "--results-dir", a.results_dir, "--provider", a.provider]
    llm_flag = ["--use-llm"] if use_llm else []
    try:
        for mod, name in ((etc, "evaluation_report"), (epr, "pass_rate_report")):
            sys.argv = [mod.__file__, *common, "--output", str(out / f"{a.provider}_{name}.json"), *llm_flag]
            print(f"\n=== {mod.__name__} (judge={judge}) ===")
            mod.main()
        report = json.loads((out / f"{a.provider}_pass_rate_report.json").read_text())
        inventory["evaluated_files"] = report["total_scenarios"]
        metadata["tool_evaluation_complete"] = True
        if judge == "openai":  # the upstream latency analyzer uses its own client
            subprocess.run([sys.executable, str(V3 / "analyze_tool_latency.py"), "--results-dir", a.results_dir,
                            "--provider", a.provider, "--output", str(out / f"{a.provider}_latency_report.json")],
                           check=True)
            metadata["latency_judge"] = "gpt-4o alias; requests not included in counts above"
        metadata["evaluation_complete"] = True
    finally:
        sys.argv = invocation
        (out / "judge.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (out / "evaluation_inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
        if use_llm:
            client.client.close()


if __name__ == "__main__":
    main()
