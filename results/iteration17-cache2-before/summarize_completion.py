"""Separate finished audio from fulfilled final requested work; preserve every case."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from summarize_latency import metric

BASE = Path(__file__).resolve().parent


def completion_metric(values):
    result = metric(values)
    result["failed_or_unmeasured"] = result.pop("missing_or_interrupted")
    return result


def main():
    output = BASE / "completion-latency.json"
    assert not output.exists(), "Preserve existing evidence."
    sources = [BASE / "protocol.json", BASE / "audit.json", BASE / "latency-summary.json",
               BASE / "received-audio-review/qualitative-review.json", Path(__file__).resolve(),
               BASE / "summarize_latency.py"]
    protocol, audit, latency, speech = [json.loads(p.read_text()) for p in sources[:4]]
    counts, rows = {}, []
    for case in protocol["run_order"]:
        counts[case] = counts.get(case, 0) + 1
        name = f"rtc-{case}-{counts[case]}"
        version = len(protocol["expected"][case]["turns"])
        trial = next((t for t in speech["trials"] if t["id"] == name), {})
        timed = next((t for t in latency["per_turn"] if t["trial"] == name and t["intent_version"] == version), {})
        core = next((t for t in audit["trials"] if t["name"] == name), {})
        checks = core.get("semantic_checks", {})
        semantic = bool(checks) and all(value is True for value in checks.values())
        assert trial.get("semantic_effect_case_passed") is semantic, "Speech review/backend audit disagree"
        spoken = trial.get("manual_review", {}).get("complete_declared_final_speech") is True
        finished = timed.get("result_handle_state") == "finished"
        observed = timed.get("speech_end_to_client_observation_ms", {}).get("result_handle_finished_ms")
        rows.append({"trial": name, "case": case, "final_intent_version": version,
                     "semantic_case_passed": semantic, "all_declared_final_speech_facts": spoken,
                     "result_handle_finished": finished, "observed_finished_reply_ms": observed,
                     "fulfilled_case_reply_finish_ms": observed if semantic and spoken and finished else None})
    receipt = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
               "scope": "Final-input speech end to result-handle finish, conditioned on the whole declared case's backend effects and all final spoken facts. Failed/missing cases stay null in the full denominator.",
               "per_case": rows,
               "groups": {name: completion_metric([r["fulfilled_case_reply_finish_ms"] for r in rows if name == "all" or r["case"] == name])
                          for name in ["all", *protocol["expected"]]},
               "source_sha256": {str(p.relative_to(BASE)): sha256(p.read_bytes()).hexdigest() for p in sources},
               "limits": ["This does not establish physical audible completion or human intelligibility.",
                          "An unfulfilled but finished reply remains visible separately from task completion.",
                          "Case-level success requires every declared effect; this statistic starts at the final input, not the first input of the case."]}
    with output.open("x") as f:
        json.dump(receipt, f, indent=2)
        f.write("\n")
    print(json.dumps(receipt["groups"], indent=2))


if __name__ == "__main__":
    main()
