"""Text-level dev loop: feed our synthetic disfluent transcripts through the
real coordinator + real LLM, then score the executed tool calls with the same
strict rule the benchmark uses (exact tool multiset + argument match).

    python -m tests.synthetic.run_synthetic --profile kimi [--only syn_fin_02] [-j 4]

No audio, no LiveKit, no benchmark data involved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from agent.config import load_config, make_coordinator_config, make_llm
from agent.coordinator.coordinator import Coordinator
from agent.coordinator.events import EventBus
from agent.coordinator.executor import Executor
from agent.coordinator.ledger import Ledger
from agent.coordinator.resolver import IntentResolver
from agent.tools.bench_tools import build_bench_manifest

HERE = Path(__file__).parent


def _norm(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower().strip()
    s = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    try:
        return float(s)
    except ValueError:
        return s


def _date_equal(a: str, b: str) -> bool:
    months = "january february march april may june july august september october november december".split()
    def parts(x: str):
        x = str(x).lower()
        m = next((i for i, mo in enumerate(months) if mo in x or mo[:3] in x), None)
        d = re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", x)
        return m, (int(d[-1]) if d else None)
    return parts(a) == parts(b)


def args_match(expected: dict, actual: dict) -> tuple[bool, str]:
    for k, ev in expected.items():
        if k not in actual:
            return False, f"missing {k}"
        if ev == "*":
            continue
        av = actual[k]
        if k == "date" and _date_equal(ev, av):
            continue
        if _norm(ev) != _norm(av):
            ne, na = _norm(ev), _norm(av)
            if isinstance(ne, str) and isinstance(na, str) and (ne in na or na in ne) and min(len(ne), len(na)) >= 4:
                continue  # "central library" vs "the central library"
            return False, f"{k}: expected {ev!r} got {av!r}"
    return True, ""


def score(expected: list[dict], actual: list[tuple[str, dict]]) -> tuple[bool, str]:
    exp_names = Counter(e["tool"] for e in expected)
    act_names = Counter(a[0] for a in actual)
    if exp_names != act_names:
        return False, f"tools expected {dict(exp_names)} got {dict(act_names)}"
    pool = list(actual)
    for e in expected:
        cands = [a for a in pool if a[0] == e["tool"]]
        ok_any = False
        why = ""
        for a in cands:
            ok, why = args_match(e["args"], a[1])
            if ok:
                pool.remove(a)
                ok_any = True
                break
        if not ok_any:
            return False, f"{e['tool']} args: {why}"
    return True, ""


async def run_one(sc: dict, cfg: dict, verbose: bool) -> dict:
    manifest = build_bench_manifest("instant")
    bus, ledger = EventBus(), Ledger()
    calls: list[tuple[str, dict]] = []
    ex = Executor(manifest, ledger, bus, lambda f, a, t0, t1: calls.append((f, a)))
    said: list[str] = []

    async def speak(t: str) -> None:
        said.append(t)

    llm = make_llm(cfg)
    coord = Coordinator(IntentResolver(llm, manifest), ex, ledger, bus, speak, None,
                        make_coordinator_config(cfg))
    coord.cfg.speak_results = False
    gap = sc.get("gap_ms", 800) / 1000
    t0 = time.time()
    for turn in sc["turns"]:
        segs = turn[0] if turn and isinstance(turn[0], list) else turn
        for i, seg in enumerate(segs):
            if i > 0:
                await asyncio.sleep(gap)
                coord.on_user_speaking()
            coord.on_user_turn(seg)
        await asyncio.sleep(0.05)
        await coord.drain(timeout=60)
    ok, why = score(sc["expected"], calls)
    plans = [e.data for e in bus.history if e.type == "plan_ready"]
    return {"id": sc["id"], "pass": ok, "why": why, "calls": calls, "said": said,
            "secs": round(time.time() - t0, 1), "plans": plans if (verbose or not ok) else None}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("-j", type=int, default=1)
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cfg = load_config(a.profile)
    scs = yaml.safe_load((HERE / "scenarios.yaml").read_text())
    if a.only:
        scs = [s for s in scs if a.only in s["id"]]
    sem = asyncio.Semaphore(a.j)

    async def guarded(sc):
        async with sem:
            return await run_one(sc, cfg, a.v)

    res = await asyncio.gather(*(guarded(s) for s in scs))
    for r in res:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"{mark} {r['id']:<16} {r['secs']:>5}s  {r['why']}")
        if r["plans"]:
            for p in r["plans"]:
                print("      plan:", json.dumps({k: p.get(k) for k in ("utterance", "complete", "calls", "reply")}, ensure_ascii=False)[:400])
    n = sum(r["pass"] for r in res)
    print(f"\nprofile={cfg['profile']} model={cfg['llm']['model']}  pass {n}/{len(res)} = {n / len(res):.0%}")
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main())
