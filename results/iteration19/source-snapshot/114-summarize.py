"""Print a markdown summary of one run directory (results/run_*)."""
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
pr = next(out.glob("*_pass_rate_report.json"), None)
ev = next(out.glob("*_evaluation_report.json"), None)
judge = json.loads((out / "judge.json").read_text()) if (out / "judge.json").exists() else {}
print(f"# Run {out.name}\n\njudge: {judge.get('judge')} {judge.get('model') or ''}\n")
if pr:
    r = json.loads(pr.read_text())
    print(f"**Strict pass rate: {r['overall_pass_rate']:.1%}** ({r['passed']}/{r['total_scenarios']})\n")
    for k in ("by_difficulty", "by_domain", "by_disfluency_feature", "by_state_rollback", "failure_breakdown"):
        print(f"- {k}: {json.dumps(r.get(k))}")
if ev:
    r = json.loads(ev.read_text())
    flat = {k: v for k, v in r.items() if not isinstance(v, (list,))}
    print("\n## Tool-call evaluation\n")
    for k, v in flat.items():
        print(f"- {k}: {json.dumps(v)[:300]}")
