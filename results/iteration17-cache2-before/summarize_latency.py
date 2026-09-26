"""Summarize all declared turns and interruption pairs without dropping failures."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path

BASE = Path(__file__).resolve().parent


def metric(values):
    valid = sorted(v for v in values if v is not None)
    return {"declared": len(values), "n": len(valid), "missing_or_interrupted": len(values) - len(valid),
            "values_ms_in_run_order": values,
            "p50_ms": valid[math.ceil(len(valid) * .5) - 1] if valid else None,
            "p95_ms": valid[math.ceil(len(valid) * .95) - 1] if valid else None}


def main():
    output = BASE / "latency-summary.json"
    assert not output.exists(), "Preserve existing evidence."
    audit = json.loads((BASE / "audit.json").read_text())
    protocol = json.loads((BASE / "protocol.json").read_text())
    rows, pairs = [], []
    names = ["transcript_ms", "ack_text_ms", "result_text_ms", "first_tool_intention_ms",
             "mutation_intention_ms", "mutation_success_ms", "first_ack_pcm_ms",
             "first_result_pcm_ms", "result_handle_finished_ms"]
    counts = {}
    for case in protocol["run_order"]:
        counts[case] = counts.get(case, 0) + 1
        trial_name = f"rtc-{case}-{counts[case]}"
        trial = next((r for r in audit["trials"] if r["name"] == trial_name), {})
        capture_path = BASE / trial_name / "report.json"
        capture = json.loads(capture_path.read_text()) if capture_path.exists() else {}
        for version, declared in enumerate(protocol["expected"][case]["turns"], 1):
            turn = next((t for t in trial.get("turns", []) if t["intent_version"] == version), {})
            measures = turn.get("speech_end_to_client_observation_ms", {})
            handle = next((h for h in capture.get("speech_handles", [])
                           if h.get("kind") == "result" and h.get("intent_version") == version), {})
            row = {"trial": trial_name, "case": case, "intent_version": version,
                   "declared_text": declared["text"], "input": turn.get("input"),
                   "result_handle_state": handle.get("state"),
                   "speech_end_to_client_observation_ms": {name: measures.get(name) for name in names}}
            for key, target in [("tool_success_to_result_text_ms", "result_text_ms"),
                                ("tool_success_to_result_pcm_ms", "first_result_pcm_ms")]:
                left, right = measures.get(target), measures.get("mutation_success_ms")
                row[key] = round(left - right, 3) if left is not None and right is not None else None
            rows.append(row)
        for source, target in protocol["expected"][case]["required_interruption_pairs"]:
            pair = next((p for p in trial.get("interruptions", [])
                         if p["from_intent_version"] == source and p["to_intent_version"] == target), {})
            pairs.append({"trial": trial_name, "from_intent_version": source, "to_intent_version": target,
                          **{name: pair.get(name) for name in ("old_handle_state", "last_old_pcm_after_input_onset_ms",
                              "handle_interruption_after_input_onset_ms", "untagged_tail_after_recorder_last_ms")}})
    groups = {"initial_requests": [r for r in rows if r["intent_version"] == 1],
              "all_followups": [r for r in rows if r["intent_version"] > 1],
              "return_to_airport": [r for r in rows if r["intent_version"] == 3], "all_turns": rows}
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
              "method": "Nearest rank: sorted valid values[ceil(n*p)-1]; all declared rows and null counts retained. No exclusion or causal comparison.",
              "boundary": "Client receipt relative to scheduled RMS-threshold input speech end; intention is not backend entry and PCM is not physical playback.",
              "per_turn": rows, "interruptions": pairs,
              "speech_end_groups": {group: {name: metric([r["speech_end_to_client_observation_ms"][name] for r in items])
                                             for name in names} for group, items in groups.items()},
              "phases": {name: metric([r[name] for r in rows]) for name in
                         ("tool_success_to_result_text_ms", "tool_success_to_result_pcm_ms")},
              "interruption_metrics": {name: metric([p[name] for p in pairs]) for name in
                                       ("last_old_pcm_after_input_onset_ms", "handle_interruption_after_input_onset_ms")},
              "source_sha256": {p.name: sha256(p.read_bytes()).hexdigest() for p in
                                (Path(__file__), BASE / "protocol.json", BASE / "audit.json")},
              "limits": ["Repeated synthetic development inputs with shared warm models; no held-out accuracy, physical timing or population claim.",
                         "A deliberately interrupted result has no finish time. Missing responses remain null regardless of cause.",
                         "Interruption rows include every declared adjacent pair, including the second interruption in each three-turn repeat case."]}
    with output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"turns": len(rows), "interruption_pairs": len(pairs), "phases": result["phases"]}, indent=2))


if __name__ == "__main__":
    main()
