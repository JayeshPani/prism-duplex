"""Text-level dev loop: feed our synthetic disfluent transcripts through the
real coordinator + real LLM, then score all attempted tool calls with a conservative development scorer.
This scorer is separate from the official benchmark and is not equivalent.

    python -m tests.synthetic.run_synthetic --profile kimi [--only syn_fin_02] [-j 4]

No audio, no LiveKit, no benchmark data involved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
    # Conservative development scoring: do not erase identifier characters or
    # infer dates from partial matches. Numeric JSON values already compare equal.
    if isinstance(v, str):
        return " ".join(v.casefold().split())
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def args_match(expected: dict, actual: dict) -> tuple[bool, str]:
    if expected.keys() != actual.keys():
        return False, f"argument keys expected {sorted(expected)} got {sorted(actual)}"
    for k, ev in expected.items():
        av = actual[k]
        if ev == "*":
            continue  # explicit development expectation; never an inferred match
        if isinstance(ev, bool) != isinstance(av, bool) or _norm(ev) != _norm(av):
            return False, f"{k}: expected {ev!r} got {av!r}"
    return True, ""


def score(expected: list[dict], actual: list[tuple[str, dict]]) -> tuple[bool, str]:
    exp_names = Counter(e["tool"] for e in expected)
    act_names = Counter(a[0] for a in actual)
    if exp_names != act_names:
        return False, f"tools expected {dict(exp_names)} got {dict(act_names)}"
    # Match the multiset bijectively; a wildcard must not consume the only
    # candidate for a more specific expectation.
    owners: dict[int, int] = {}

    def match(i: int, seen: set[int]) -> bool:
        e = expected[i]
        for j, (tool, args) in enumerate(actual):
            if j in seen or tool != e["tool"] or not args_match(e["args"], args)[0]:
                continue
            seen.add(j)
            if j not in owners or match(owners[j], seen):
                owners[j] = i
                return True
        return False

    for i in range(len(expected)):
        if not match(i, set()):
            return False, f"no distinct argument match for {expected[i]!r}"
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
    coord = None
    timed_out = False
    t0 = time.time()
    try:
        coord = Coordinator(IntentResolver(llm, manifest), ex, ledger, bus, speak, None,
                            make_coordinator_config(cfg))
        coord.cfg.speak_results = False
        gap = sc.get("gap_ms", 800) / 1000
        for turn in sc["turns"]:
            segs = turn[0] if turn and isinstance(turn[0], list) else turn
            for i, seg in enumerate(segs):
                if i > 0:
                    await asyncio.sleep(gap)
                    coord.on_user_speaking()
                coord.on_user_turn(seg)
            await asyncio.sleep(0.05)
            await coord.drain(timeout=60)
    except TimeoutError:
        timed_out = True
    finally:
        try:
            if coord is not None:
                await coord.aclose()
        finally:
            await llm.client.close()
    ok, why = (False, "coordinator timeout") if timed_out else score(sc["expected"], calls)
    plans = [e.data for e in bus.history if e.type == "plan_ready"]
    return {"id": sc["id"], "pass": ok, "why": why, "calls": calls, "said": said,
            "secs": round(time.time() - t0, 1), "plans": plans if (verbose or not ok) else None,
            "events": [e.to_dict() for e in bus.history]}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("-j", type=int, default=1)
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.j <= 0:
        ap.error("-j must be a positive integer")
    scs = yaml.safe_load((HERE / "scenarios.yaml").read_text()) or []
    if a.only:
        scs = [s for s in scs if a.only in s["id"]]
    if not scs:
        ap.error("no scenarios selected")
    cfg = load_config(a.profile)
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
