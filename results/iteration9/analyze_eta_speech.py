"""Reproduce the original ETA experiment audit without loading speech models.

Manual annotations below are tied to exact reviewed transcripts. A different
transcript requires a new review, rather than inheriting an old case judgment.
"""
from array import array
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys
import wave

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
DATA = BASE / "eta-speech"

# All sixteen rows were read against their declared text. Both occurrences of
# each arm/case have the exact transcript recorded here; assert that below.
REVIEWED = {
    ("A", "airport_start"): ("Navigation to Kempegauda International Airport has started. EDA, 88 minutes.", 88, "EDA"),
    ("B", "airport_start"): ("Navigation to Kempegauda International Airport has started. Estimated arrival in 88 minutes.", 88, "Estimated arrival"),
    ("A", "office_replacement"): ("Now navigating to office, Maniata Tech Park, replacing Kempegauda International Airport. Ida, 29 minutes.", 29, "Ida"),
    ("B", "office_replacement"): ("Now navigating to office, Maniata Tech Park, replacing Kempegauda International Airport. Estimated arrival in 29 minutes.", 29, "Estimated arrival"),
    ("A", "add_stop"): ("Added blue Takai in Durinagar as a stop. Eda to MG Road Metro Station, 30 minutes, 0 extra minutes.", 30, "Eda"),
    ("B", "add_stop"): ("Added blue Takai in Durinagar as a stop. Estimated time to MG Road Metro Station: 30 minutes, 0 extra minutes.", 30, "Estimated time"),
    ("A", "repeat_stop"): ("The stop at Blue Takai, in Durinager, is already on your route. Eda to Office, Maniata Tech Park, 31 minutes.", 31, "Eda"),
    ("B", "repeat_stop"): ("The stop at Blue Takeai, in Durinagar, is already on your route. Estimated time to office, Maniata Tech Park, 31 minutes.", 31, "Estimated time"),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def descriptive(values):
    return {"n": len(values), "values_in_order": values, "mean": statistics.mean(values),
            "median_midpoint": statistics.median(values), "min": min(values), "max": max(values)}


def wav_data(path, rate):
    with wave.open(str(path), "rb") as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) == (1, 2, rate)
        data = stream.readframes(stream.getnframes())
        assert len(data) == stream.getnframes() * 2
        return data


def main():
    report = json.loads((DATA / "report.json").read_text())
    protocol = json.loads((DATA / "protocol.json").read_text())
    supervision = json.loads((BASE / "eta-supervision.json").read_text())
    rows, cases = report["trials"], {case["id"]: case for case in protocol["cases"]}
    assert report["status"] == "completed" and len(rows) == 16
    expected_order = [(block, arm, case) for block, arm in enumerate(protocol["arm_order"], 1) for case in cases]
    assert expected_order == [(row["block"], row["arm"], row["case"]) for row in rows]
    reviewed, artifact_hashes, pcm16 = [], {}, {}
    for row in rows:
        case = cases[row["case"]]
        transcript, observed_minutes, label = REVIEWED[row["arm"], row["case"]]
        assert row["status"] == "completed" and row["transcript"] == transcript
        assert row["text"] == case["texts"][row["arm"]] and row["expected_minutes"] == case["expected_minutes"]
        assert re.search(rf"\b{observed_minutes} minutes\b", transcript)
        for kind in ("pcm24", "wav24", "wav16"):
            path = DATA / row[kind]["file"]
            artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
            assert artifact_hashes[str(path.relative_to(ROOT))] == row[kind]["sha256"]
        raw = (DATA / row["pcm24"]["file"]).read_bytes()
        assert raw == wav_data(DATA / row["wav24"]["file"], 24000)
        pcm16[row["id"]] = wav_data(DATA / row["wav16"]["file"], 16000)
        assert len(raw) // 2 == row["pcm24"]["samples"]
        assert len(pcm16[row["id"]]) // 2 == row["wav16"]["samples"]
        assert len(raw) / 48000 == row["pcm24"]["duration_seconds"]
        assert len(pcm16[row["id"]]) / 32000 == row["wav16"]["duration_seconds"]
        names = []
        if row["case"] in ("airport_start", "office_replacement"):
            names.append("Kempegowda → Kempegauda")
        if row["case"] in ("office_replacement", "repeat_stop"):
            names.append("Manyata → Maniata")
        if row["case"] in ("add_stop", "repeat_stop"):
            names.append("Blue Tokai → Blue Takeai" if row["arm"] == "B" and row["case"] == "repeat_stop" else "Blue Tokai → Blue Takai")
            names.append("Indiranagar → in Durinager" if row["arm"] == "A" and row["case"] == "repeat_stop" else "Indiranagar → in Durinagar")
        action = {"airport_start": "has started", "office_replacement": "replacing Kempegauda International Airport",
                  "add_stop": "as a stop", "repeat_stop": "is already on your route"}[row["case"]]
        assert action in transcript
        reviewed.append({**row, "manual_review": {
            "observed_estimate_label": label,
            "declared_label_preserved": (bool(re.search(r"\beta\b", transcript, re.I)) if row["arm"] == "A"
                                         else case["expected_candidate_label"].lower() in transcript.lower()),
            "observed_minutes": observed_minutes, "minutes_fact_preserved": observed_minutes == case["expected_minutes"],
            "number_deviation": "13 → 30 minutes" if observed_minutes != case["expected_minutes"] else None,
            "proper_name_deviations": names, "exact_proper_names": not names,
            "action_relation_present": True, "action_relation_evidence": action,
            "replacement_relation_present": True if row["case"] == "office_replacement" else None,
            "already_present_noop_preserved": True if row["case"] == "repeat_stop" else None,
            "zero_extra_minutes_preserved": "0 extra minutes" in transcript if row["case"] == "add_stop" else None,
            "limit": "ASR transcript observation; does not attribute differences to TTS versus ASR or certify intelligibility."}})
    pairs, case_metrics = [], {}
    for case_id in cases:
        groups = {arm: [row for row in rows if row["case"] == case_id and row["arm"] == arm] for arm in ("A", "B")}
        case_metrics[case_id] = {arm: {"tts_seconds": descriptive([row["tts_seconds"] for row in group]),
                                     "speech_duration_seconds": descriptive([row["pcm24"]["duration_seconds"] for row in group])}
                                 for arm, group in groups.items()}
        case_metrics[case_id]["B_minus_A_mean_tts_seconds"] = statistics.mean(row["tts_seconds"] for row in groups["B"]) - statistics.mean(row["tts_seconds"] for row in groups["A"])
        case_metrics[case_id]["B_minus_A_speech_duration_seconds"] = groups["B"][0]["pcm24"]["duration_seconds"] - groups["A"][0]["pcm24"]["duration_seconds"]
        for arm, (first, second) in groups.items():
            arrays = [array("h", pcm16[row["id"]]) for row in (first, second)]
            if sys.byteorder != "little":
                for values in arrays:
                    values.byteswap()
            differences = [abs(a - b) for a, b in zip(*arrays)]
            assert len(arrays[0]) == len(arrays[1])
            pairs.append({"case": case_id, "arm": arm, "trial_ids": [first["id"], second["id"]],
                          "pcm24_bytes_identical": first["pcm24"]["sha256"] == second["pcm24"]["sha256"],
                          "wav24_bytes_identical": first["wav24"]["sha256"] == second["wav24"]["sha256"],
                          "wav16_bytes_identical": first["wav16"]["sha256"] == second["wav16"]["sha256"],
                          "wav16_equal_sample_counts": len(arrays[0]),
                          "wav16_unequal_samples": sum(value > 0 for value in differences),
                          "wav16_max_absolute_sample_difference": max(differences),
                          "transcripts_identical": first["transcript"] == second["transcript"]})
    receipts = {}
    for key in ("source_hashes_before", "source_hashes_after", "model_hashes_before", "model_hashes_after", "protocol_hashes_after"):
        records = report[key]
        receipts[key] = {"declared_files": len(records), "internally_consistent_matches": all(
            record.get("matches") is True and record.get("expected") == record.get("observed") for record in records.values())}
    current_source_mismatches = [name for name, record in report["source_hashes_after"].items() if sha(ROOT / name) != record["expected"]]
    counts, metrics = {}, {}
    for arm in ("A", "B"):
        group = [row for row in reviewed if row["arm"] == arm]
        counts[arm] = {"declared_rows": 8, "completed_rows": len(group),
                       "declared_estimate_label_preserved": sum(row["manual_review"]["declared_label_preserved"] for row in group),
                       "primary_minutes_fact_preserved": sum(row["manual_review"]["minutes_fact_preserved"] for row in group),
                       "primary_minutes_fact_failed": sum(not row["manual_review"]["minutes_fact_preserved"] for row in group),
                       "rows_with_proper_name_deviations": sum(bool(row["manual_review"]["proper_name_deviations"]) for row in group),
                       "action_relations_present": sum(row["manual_review"]["action_relation_present"] for row in group),
                       "replacement_relations": {"present": 2, "applicable": 2},
                       "already_present_noop": {"present": 2, "applicable": 2},
                       "zero_extra_minutes": {"present": 2, "applicable": 2}}
        metrics[arm] = {key: descriptive([row[key] for row in group]) for key in ("tts_seconds", "stt_seconds", "resample_seconds", "total_trial_seconds")}
        metrics[arm]["speech_duration_seconds"] = descriptive([row["pcm24"]["duration_seconds"] for row in group])
    output = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "All16 declared completed Kokoro→Parakeet ABBA rows, exact current transcripts and saved audio; no inference, tests, playback or RTC analysis.",
              "trial_order_verified": True, "counts": counts, "reviewed_trials": reviewed,
              "descriptive_metrics_by_arm": metrics, "descriptive_metrics_by_case": case_metrics,
              "descriptive_metrics_by_block": {str(block): {"arm": arm, "tts_seconds": descriptive([row["tts_seconds"] for row in rows if row["block"] == block])}
                                                 for block, arm in enumerate(protocol["arm_order"], 1)},
              "cold_first_trial_retained": rows[0], "repeated_audio_and_transcript_pairs": pairs,
              "verification": {"raw_artifact_files_verified": len(artifact_hashes), "all_raw_hashes_match": True,
                               "pcm24_equals_wav24_payload_all16": True,
                               "protocol_copy_matches_recorded_digest": sha(DATA / "protocol.json") == report["protocol_sha256"],
                               "protocol_original_matches_recorded_digest": sha(BASE / "eta-protocol.json") == report["protocol_sha256"],
                               "frozen_hash_receipts": receipts, "current_source_mismatches": current_source_mismatches,
                               "runner_model_inventory_matches_declaration": report["model_inventory_matches_declaration"],
                               "runner_model_inventory_unchanged": report["model_inventory_unchanged"],
                               "runner_all_declared_hashes_match_after": report["all_declared_hashes_match_after"],
                               "model_verification_limit": "Model before/after receipts checked for consistency; large weights deliberately not reread while root runs RTC."},
              "cleanup_receipts": report["cleanup"], "supervision_receipt": supervision,
              "source_artifact_sha256": {str(path.relative_to(ROOT)): sha(path) for path in
                                          (DATA / "report.json", DATA / "protocol.json", BASE / "eta-protocol.json", BASE / "eta-supervision.json", Path(__file__))},
              "raw_artifact_sha256": artifact_hashes,
              "limits": protocol["limits"] + [
                  "The candidate preserves the estimate phrase in8/8, but each arm preserves primary minute values in only6/8. All four add-stop transcripts say30 instead of13; 0 extra minutes remains present. No overall quality-pass or factual-accuracy generalization follows.",
                  "Proper-name spelling deviations occur in all16. Replacement and already-present relations are retained; exact destination/venue transcription is not claimed.",
                  "Repeated TTS PCM24 is identical in all8 same-arm/case pairs; all8 resampled WAV16 pairs differ slightly, with equal sample counts and maximum absolute difference2 PCM16 units. Cause is not attributed. Their transcripts are identical.",
                  "Means, midpoint medians, ranges and all raw times are descriptive. Cold first A/airport render1.811s and STT1.117s remain included; shared order/warm-up confounds explain why a negative airport TTS delta cannot establish a speedup.",
                  "Expanded wording lengthens synthesized utterances by0.619–0.960s. This is generated sample duration, not physical speaking completion or interaction latency.",
                  "Manual annotations assert exact reviewed transcripts before reuse. Different text/transcript outputs require fresh review; do not reuse these judgments by case name alone.",
              ]}
    with (BASE / "eta-analysis.json").open("x") as stream:
        json.dump(output, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"counts": counts, "artifacts_verified": len(artifact_hashes),
                      "source_mismatches": current_source_mismatches,
                      "identical_pcm24_pairs": sum(pair["pcm24_bytes_identical"] for pair in pairs)}, indent=2))


if __name__ == "__main__":
    main()
