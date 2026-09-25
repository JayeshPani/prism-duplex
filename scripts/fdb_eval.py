"""Run the FDB-v3 evaluators (unmodified) with a configurable judge.

    python scripts/fdb_eval.py --provider prism --results-dir bench/data/fdb_v3_data_released --out results/run_x

Judge selection (JUDGE env):
  none    exact-match arguments, no response judge (free, stricter)
  openai  the official GPT-4o judge (needs OPENAI_API_KEY) - what organizers run
  local   any OpenAI-compatible server (JUDGE_BASE_URL, JUDGE_MODEL), e.g. the local Qwen
  kimi    Moonshot API (MOONSHOT_API_KEY, JUDGE_MODEL)

Non-official judges only change who answers the benchmark's own judge prompt;
scoring logic is untouched. Official numbers come from the organizers' re-run.
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="prism")
    ap.add_argument("--results-dir", default=str(ROOT / "bench" / "data" / "fdb_v3_data_released"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    judge = os.getenv("JUDGE", "none")
    use_llm = judge != "none"

    import evaluate_pass_rate as epr  # type: ignore
    import evaluate_tool_calls as etc  # type: ignore
    if use_llm and judge != "openai":
        client = _judge_client()
        epr._openai_client = client
        etc._openai_client = client

    bench = str(V3 / "benchmark_data_v2.json")
    common = ["--benchmark", bench, "--results-dir", a.results_dir, "--provider", a.provider]
    llm_flag = ["--use-llm"] if use_llm else []
    for mod, name in ((etc, "evaluation_report"), (epr, "pass_rate_report")):
        sys.argv = [mod.__file__, *common, "--output", str(out / f"{a.provider}_{name}.json"), *llm_flag]
        print(f"\n=== {mod.__name__} (judge={judge}) ===")
        mod.main()
    if judge == "openai":  # the latency analyzer calls gpt-4o directly
        subprocess.run([sys.executable, str(V3 / "analyze_tool_latency.py"), "--results-dir", a.results_dir,
                        "--provider", a.provider, "--output", str(out / f"{a.provider}_latency_report.json")],
                       check=False)
    (out / "judge.json").write_text(json.dumps({"judge": judge, "model": os.getenv("JUDGE_MODEL", "gpt-4o" if judge == "openai" else None)}))


if __name__ == "__main__":
    main()
