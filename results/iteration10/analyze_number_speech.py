"""Reproduce the bounded, manually guarded audit of the frozen 60-trial run.

No model imports or inference. A changed protocol or transcript requires a new
manual review before these semantic annotations can be reused. Existing output
is refused; --out permits an independently named reproduction artifact.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import wave

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROTOCOL_SHA256 = "961db1f03eca6f3c801d0e947d5055504be03613a7a52ea81fcccbcc6f5e2011"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def reviewed_transcript(arm, case):
    """Exact text manually read for every row, including all number errors."""
    if case.startswith("added_stop_"):
        requested = int(case.removeprefix("added_stop_"))
        if arm == "A":
            name = "blue talkai" if requested == 14 else "blue Takai"
            spoken = 30 if requested == 13 else requested
            return (f"Added {name} in Durinagar as a stop. Estimated time to MG Road "
                    f"Metro Station: {spoken} minutes, 0 extra minutes.")
        if arm == "B":
            location = "Durinager" if requested == 16 else "Durinegar"
            punctuation = "," if requested in (13, 14, 16) else ":"
            spoken = 60 if requested == 16 else requested
            extra = "zero" if requested in (40, 60) else "0"
            return (f"Added blue talkai in {location} as a stop. Estimated time to MG Road "
                    f"Metro Station{punctuation} {spoken} minutes, {extra} extra minutes.")
        return ("Added blue Takai in Durinagar as a stop. Estimated time to MG Road "
                f"Metro Station is {requested} minutes, 0 extra minutes.")
    if case == "repeat_stop_31":
        name = "Takai" if arm == "B" else "Takeai"
        connective = "is " if arm == "C" else ""
        return (f"The stop at Blue {name}, in Durinagar, is already on your route. "
                f"Estimated time to office, Maniata Tech Park, {connective}31 minutes.")
    return {
        "harbor_start_13": "Navigation to Harbor Museum has started. Estimated arrival in 13 minutes.",
        "airport_start_88": "Navigation to Kempegauda International Airport has started. Estimated arrival in 88 minutes.",
        "office_replacement_29": "Now navigating to office, Maniata Tech Park, replacing Kempegauda International Airport. Estimated arrival in 29 minutes.",
    }[case]


def semantic_review(arm, case):
    """Apply only after the exact transcript guard; numbers are scored separately."""
    if case.startswith("added_stop_"):
        name = "blue talkai" if arm == "B" or (arm == "A" and case == "added_stop_14") else "blue Takai"
        location = ("Durinager" if case == "added_stop_16" else "Durinegar") if arm == "B" else "Durinagar"
        return {
            "action_relation": "Added the named stop; MG Road Metro Station remains the ETA destination.",
            "action_relation_preserved": True, "destination_exact_words_preserved": True,
            "proper_name_deviations": {"Blue Tokai": name, "Indiranagar": location},
            "unrequested_action_or_omitted_clause_observed": False,
        }
    if case == "repeat_stop_31":
        return {
            "action_relation": "Stop already on route; no fresh-add claim; Office is the ETA destination.",
            "action_relation_preserved": True, "destination_exact_words_preserved": False,
            "proper_name_deviations": {"Blue Tokai": "Blue Takai" if arm == "B" else "Blue Takeai",
                                       "Indiranagar": "Durinagar", "Manyata": "Maniata"},
            "unrequested_action_or_omitted_clause_observed": False,
        }
    if case == "office_replacement_29":
        return {
            "action_relation": "Now navigating to Office, replacing the airport; direction of replacement preserved.",
            "action_relation_preserved": True, "destination_exact_words_preserved": False,
            "proper_name_deviations": {"Manyata": "Maniata", "Kempegowda": "Kempegauda"},
            "unrequested_action_or_omitted_clause_observed": False,
        }
    return {
        "action_relation": "Navigation started to the stated museum or airport.",
        "action_relation_preserved": True,
        "destination_exact_words_preserved": case == "harbor_start_13",
        "proper_name_deviations": {} if case == "harbor_start_13" else {"Kempegowda": "Kempegauda"},
        "unrequested_action_or_omitted_clause_observed": False,
    }


def stats(values):
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "min": min(values), "max": max(values), "total": sum(values)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=HERE / "number-analysis.json")
    args = parser.parse_args()
    require(not args.out.exists(), "refusing existing output")
    protocol_path = HERE / "number-protocol.json"
    report_path = HERE / "number-speech/report.json"
    require(sha(protocol_path) == PROTOCOL_SHA256, "changed protocol requires new manual review")
    p, r = json.loads(protocol_path.read_text()), json.loads(report_path.read_text())
    supervision_path = HERE / "number-supervision.json"
    supervision = json.loads(supervision_path.read_text())
    require(r["status"] == "completed" and supervision["exit_code"] == 0, "run not completed cleanly")
    require(r["protocol_sha256"] == PROTOCOL_SHA256, "report protocol differs")
    require(sha(HERE / "number-speech/protocol.json") == PROTOCOL_SHA256, "copied protocol differs")
    expected_order = [(block, arm, case) for block, arm in enumerate(p["arm_order"], 1) for case in p["cases"]]
    require(len(r["trials"]) == len(expected_order) == 60, "missing or extra observations")
    alias_value = {alias.casefold(): int(value) for value, aliases in p["number_aliases"].items() for alias in aliases}
    alias_pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(a) for a in sorted(alias_value, key=len, reverse=True)) + r")\s+minutes\b", re.I)
    rows, audio_checks = [], []
    for t, (block, arm, case) in zip(r["trials"], expected_order):
        require((t["block"], t["arm"], t["case"]) == (block, arm, case["id"]), "observation order differs")
        require(t["status"] == "completed" and t["attempted"], "incomplete trial")
        require(t["text"] == case["texts"][arm] and t["speed"] == p["arm_speeds"][arm]
                and t["voice"] == p["configuration"]["tts"]["voice"]
                and t["expected_minutes"] == case["expected_minutes"], "trial input differs")
        require(t["transcript"] == reviewed_transcript(arm, case["id"]), f"new manual transcript review required: {t['id']}")
        matches = list(alias_pattern.finditer(t["transcript"]))
        observed = [alias_value[m.group(1).casefold()] for m in matches]
        extra_matches = re.findall(r"\b(0|zero) extra minutes\b", t["transcript"], re.I)
        review = semantic_review(arm, case["id"])
        row = {key: t[key] for key in ("id", "block", "arm", "case", "text", "transcript", "speed", "voice", "status",
                                      "expected_minutes", "tts_seconds", "stt_seconds", "total_trial_seconds", "pcm24", "wav24", "wav16")}
        row.update(primary_minutes_observed=observed,
                   primary_minutes_correct=observed == [case["expected_minutes"]],
                   matched_frozen_aliases=[m.group(1) for m in matches],
                   extra_minutes_expected=case.get("expected_extra_minutes"),
                   extra_minutes_correct=len(extra_matches) == 1 if "expected_extra_minutes" in case else None,
                   exact_reviewed_transcript_guard=True, semantic_review=review)
        rows.append(row)
        checks = {"id": t["id"], "files": {}}
        for kind in ("pcm24", "wav24", "wav16"):
            path = report_path.parent / t[kind]["file"]
            digest = sha(path)
            require(digest == t[kind]["sha256"], f"raw hash mismatch: {path}")
            checks["files"][kind] = {"sha256": digest, "matches": True}
        pcm = (report_path.parent / t["pcm24"]["file"]).read_bytes()
        require(len(pcm) == 2 * t["pcm24"]["samples"], "PCM sample count differs")
        require(len(pcm) / 48000 == t["pcm24"]["duration_seconds"], "PCM duration differs")
        for kind, rate in (("wav24", 24000), ("wav16", 16000)):
            with wave.open(str(report_path.parent / t[kind]["file"]), "rb") as f:
                require((f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, rate), "WAV format differs")
                samples, payload = f.getnframes(), f.readframes(f.getnframes())
            if kind == "wav24":
                require(payload == pcm, "WAV24 payload differs from raw PCM24")
            else:
                require(samples == t[kind]["samples"] and samples / rate == t[kind]["duration_seconds"], "WAV16 duration differs")
                require(abs(samples / rate - len(pcm) / 48000) <= 1 / rate, "resampled duration drift")
        checks["wave_format_payload_and_duration_valid"] = True
        audio_checks.append(checks)

    by_arm, by_case, repeats = {}, {}, []
    for arm in ("A", "B", "C"):
        selected = [row for row in rows if row["arm"] == arm]
        by_arm[arm] = {
            "observations": len(selected), "primary_minutes_correct": sum(x["primary_minutes_correct"] for x in selected),
            "failed_ids": [x["id"] for x in selected if not x["primary_minutes_correct"]],
            "extra_minutes_applicable": sum(x["extra_minutes_correct"] is not None for x in selected),
            "extra_minutes_correct": sum(x["extra_minutes_correct"] is True for x in selected),
            "action_relations_preserved": sum(x["semantic_review"]["action_relation_preserved"] for x in selected),
            "destination_exact_words_preserved": sum(x["semantic_review"]["destination_exact_words_preserved"] for x in selected),
            "rows_with_proper_name_deviations": sum(bool(x["semantic_review"]["proper_name_deviations"]) for x in selected),
            "generated_speech_seconds": stats([x["pcm24"]["duration_seconds"] for x in selected]),
            "tts_seconds": stats([x["tts_seconds"] for x in selected]),
            "stt_seconds": stats([x["stt_seconds"] for x in selected]),
        }
    for case in p["cases"]:
        case_id = case["id"]
        by_case[case_id] = {}
        for arm in ("A", "B", "C"):
            selected = [row for row in rows if row["case"] == case_id and row["arm"] == arm]
            by_case[case_id][arm] = {
                "primary_minutes_correct": sum(x["primary_minutes_correct"] for x in selected), "observations": len(selected),
                "observed_minutes": [x["primary_minutes_observed"] for x in selected],
                "generated_speech_seconds": stats([x["pcm24"]["duration_seconds"] for x in selected]),
                "tts_seconds": stats([x["tts_seconds"] for x in selected]),
            }
            repeats.append({"case": case_id, "arm": arm, "ids": [x["id"] for x in selected],
                            **{kind + "_byte_identical": len({x[kind]["sha256"] for x in selected}) == 1
                               for kind in ("pcm24", "wav24", "wav16")},
                            "transcripts_identical": len({x["transcript"] for x in selected}) == 1})
        for arm in ("B", "C"):
            for metric in ("generated_speech_seconds", "tts_seconds"):
                by_case[case_id][arm][metric + "_mean_delta_vs_A"] = by_case[case_id][arm][metric]["mean"] - by_case[case_id]["A"][metric]["mean"]

    eligibility = {}
    for arm in ("B", "C"):
        lost = [case for case, values in by_case.items() if values["A"]["primary_minutes_correct"] == 2 and values[arm]["primary_minutes_correct"] != 2]
        greater = by_arm[arm]["primary_minutes_correct"] > by_arm["A"]["primary_minutes_correct"]
        eligibility[arm] = {"strictly_more_correct_than_A": greater, "A_preserved_cases_lost": lost, "qualifies": greater and not lost}
    selected_arm = next((arm for arm in ("C", "B") if eligibility[arm]["qualifies"]), "A")
    receipts = {}
    for key, declared in (("source_hashes_before", p["frozen_sha256"]), ("source_hashes_after", p["frozen_sha256"]),
                          ("model_hashes_before", p["model_files_sha256"]), ("model_hashes_after", p["model_files_sha256"])):
        values = r[key]
        require(set(values) == set(declared), f"receipt inventory differs: {key}")
        require(all(v["matches"] and v["expected"] == v["observed"] == declared[k] for k, v in values.items()), f"receipt mismatch: {key}")
        receipts[key] = {"count": len(values), "all_match_declared": True}
    current_sources = {name: {"expected": digest, "observed": sha(ROOT / name)} for name, digest in p["frozen_sha256"].items()}
    for value in current_sources.values():
        value["matches"] = value["expected"] == value["observed"]
    blocks = [{"block": block, "arm": arm, "observations": 10,
               "primary_minutes_correct": sum(x["primary_minutes_correct"] for x in rows if x["block"] == block),
               "tts_seconds": stats([x["tts_seconds"] for x in rows if x["block"] == block])}
              for block, arm in enumerate(p["arm_order"], 1)]
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Manual text/transcript review plus arithmetic/hash audit of all 60 fixed offline observations; no audio listening, inference or general quality score.",
        "inputs_sha256": {str(path.relative_to(ROOT)): sha(path) for path in (protocol_path, report_path, supervision_path, Path(__file__))},
        "counts": {"declared": 60, "reviewed": len(rows), "completed": 60, "raw_audio_hashes_verified": 3 * len(audio_checks)},
        "number_method": "Require exactly one primary duration matching a frozen protocol alias immediately before 'minutes'. Zero extra minutes is separately reviewed from literal '0 extra minutes' or 'zero extra minutes'.",
        "selection": {"predeclared_rule": p["selection_rule"], "eligibility": eligibility, "selected_arm": selected_arm,
                      "interpretation": "C repairs both observed 13-to-30 errors without losing a preserved case; B repairs 13 but loses 16 to 60 and has no net numeric gain. All action relations and extra-minute clauses remain represented, with continuing proper-name spelling deviations."},
        "by_arm": by_arm, "by_case": by_case, "by_block": blocks,
        "all_rows": rows, "same_arm_case_repeats": repeats,
        "repeat_summary": {key: sum(x[key] for x in repeats) for key in ("pcm24_byte_identical", "wav24_byte_identical", "wav16_byte_identical", "transcripts_identical")},
        "duration_scope": "Generated whole-utterance duration from PCM sample count, not live delivery or human-perceived latency. Stage times retain the cold first inference and fixed-order warm-process effects.",
        "hash_audit": {"raw_audio": audio_checks, "frozen_runner_receipts": receipts, "current_small_source_hashes": current_sources,
                       "model_weights_reread_by_this_audit": False, "model_note": "Before/after runner receipts are checked against the frozen declarations; large model files were not reread during live RTC work.",
                       "reported_all_declared_hashes_match_after": r["all_declared_hashes_match_after"],
                       "reported_model_inventory_unchanged": r["model_inventory_unchanged"]},
        "supervision": supervision, "model_pool_cleanup": r["cleanup"],
        "limits": [
            "Failure-informed development on ten declared cases, not a held-out evaluation; 20 observations per arm are two repeats of ten cases.",
            "All 30 same-arm/case PCM pairs are byte-identical and their transcripts repeat; these are dependent outputs, not 60 independent accuracy samples.",
            "One Kokoro voice and one Parakeet recognizer are an ASR proxy. Number or proper-name differences cannot alone localize TTS versus ASR error or establish human intelligibility.",
            "Synthetic duration substitutions do not represent tool effects; no planner, transport, microphone, VAD or user-turn latency is measured.",
            "Proper-name deviations are retained explicitly; action relation preservation does not mean exact name fidelity or an all-facts quality pass.",
            "ABCCBA order, shared warm models, cold first inference and uncontrolled background load limit timing comparisons; no significance, population accuracy or capacity claim.",
            "Manual semantic labels apply only to the guarded frozen protocol and exact transcripts. Changed inputs or transcripts require a new manual review.",
        ],
    }
    with args.out.open("x") as f:
        f.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"out": str(args.out), "selection": result["selection"], "by_arm": by_arm, "repeat_summary": result["repeat_summary"]}, indent=2))


if __name__ == "__main__":
    main()
