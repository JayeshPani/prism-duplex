"""Guarded manual review of four complete received streams; no new inference."""
from array import array
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import wave

BASE = Path(__file__).resolve().parent
GUARDS = {
    "protocol.json": "1b9a04c94ce883430feb59a92517c8a7ebe4cb984e1c9fedd3ae79af6ade4a65",
    "received-audio-review-protocol.json": "4d7c69d8dd6e86aad49f8d941140cca139c3b02536b6082051ab1231e9cbccaa",
    "received-audio-review/protocol.json": "6bf5eae912cc421256f1b19bb14c7976b384df62384efddaff03f15348cfa0f7",
    "received-audio-review/report.json": "82cb6cafcb9c9baccfba82d46a152c7f8e0fe13007ad2c9cca4ed159ce5c5332",
}
REVIEWED = {
    "rtc-waypoint-1": {
        "transcript": "One moment Navigation Okay, one sec Added blue Takai in Durinegar as a stop. Estimated time to MG Road Metro Station is thirteen minutes, zero extra minutes.",
        "final_minutes": 13, "final_minute_wording": "thirteen", "extra_minutes": 0,
        "proper_name_deviations": {"Blue Tokai": "blue Takai", "Indiranagar": "Durinegar"},
        "original_result_coverage": "Only 'Navigation' remains; the old destination, started clause and initial estimate are absent.",
        "other_deviations": ["Punctuation between acknowledgments and the truncated original result is absent."],
    },
    "rtc-baseline-1": {
        "transcript": "", "final_minutes": None, "final_minute_wording": None, "extra_minutes": None,
        "proper_name_deviations": {},
        "original_result_coverage": "No speech was declared. Readiness timed out before input; the received near-silent stream has no recognized content.",
        "other_deviations": [],
    },
    "rtc-waypoint-2": {
        "transcript": "One moment. Navigation Okay, one sec. Added blue talkai in Durinigar as a stop. Estimated time to MG Road Metro Station is 13 minutes, zero extra minutes.",
        "final_minutes": 13, "final_minute_wording": "13", "extra_minutes": 0,
        "proper_name_deviations": {"Blue Tokai": "blue talkai", "Indiranagar": "Durinigar"},
        "original_result_coverage": "Only 'Navigation' remains; the old destination, started clause and initial estimate are absent.",
        "other_deviations": ["There is no punctuation between 'Navigation' and the follow-up acknowledgment."],
    },
    "rtc-correction-1": {
        "transcript": "One moment. Navigation to Ok one sec. Now navigating to office, Maniata Tech Park, replacing Kempegauda International Airport. Estimated arrival in twenty-nine minutes.",
        "final_minutes": 29, "final_minute_wording": "twenty-nine", "extra_minutes": None,
        "proper_name_deviations": {"Manyata": "Maniata", "Kempegowda": "Kempegauda"},
        "original_result_coverage": "Only 'Navigation to' remains from the old result. Its airport-start and 88-minute clauses are absent; the final replacement clause separately names the airport.",
        "other_deviations": ["'Okay' is transcribed 'Ok'; punctuation boundaries differ."],
    },
}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    output = BASE / "received-audio-review/qualitative-review.json"
    require(not output.exists(), "Refuse to overwrite review.")
    for name, expected in GUARDS.items():
        require(digest(BASE / name) == expected, f"Changed evidence requires new manual review: {name}")
    report = json.loads((BASE / "received-audio-review/report.json").read_text())
    protocol = json.loads((BASE / "protocol.json").read_text())
    method = json.loads((BASE / "received-audio-review-protocol.json").read_text())
    require([t["id"] for t in report["trials"]] == list(REVIEWED) == [t["id"] for t in method["declared_trials"]], "Trial declaration differs.")
    rows, hashes = [], {}
    for trial in report["trials"]:
        name, case = trial["id"], trial["case"]
        reviewed = REVIEWED[name]
        require(trial["transcript"] == reviewed["transcript"], f"New transcript requires review: {name}")
        require(trial["status"] == "completed", "ASR row not completed.")
        for relative, expected in trial["input_sha256"].items():
            path = BASE / name / relative
            require(digest(path) == expected, f"Raw artifact differs: {path}")
            hashes[str(path.relative_to(BASE))] = expected
        wav_path = BASE / "received-audio-review" / trial["resampled"]["file"]
        require(digest(wav_path) == trial["resampled"]["sha256"], "Resampled WAV differs.")
        hashes[str(wav_path.relative_to(BASE))] = trial["resampled"]["sha256"]
        pcm = (BASE / name / trial["pcm_file"]).read_bytes()
        with wave.open(str(wav_path), "rb") as wav:
            require((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000), "WAV format differs.")
            samples = wav.getnframes()
        require(len(pcm) // 2 == trial["received_samples"] and len(pcm) % 2 == 0, "PCM length differs.")
        require(samples == trial["resampled"]["samples"], "Resampled length differs.")
        require(len(pcm) / 48000 == samples / 16000 == trial["received_duration_seconds"] == trial["resampled"]["duration_seconds"], "Whole-stream duration differs.")
        capture = json.loads((BASE / name / "report.json").read_text())
        require(capture["speech_handles"] == trial["declared_speech"], "Speech-handle copy differs.")
        results = [h for h in trial["declared_speech"] if h["kind"] == "result"]
        facts_present = name != "rtc-baseline-1"
        interruption_required = bool(protocol["expected"][case].get("followup_audio"))
        baseline_pcm = None
        if facts_present:
            require(len(results) == 2 and results[0]["state"] == "interruption_requested", "Original handle state differs.")
            require(results[-1]["state"] == "finished" and results[-1]["intent_version"] == 2, "Final result state differs.")
            require(reviewed["final_minutes"] == protocol["expected"][case]["effects"][-1]["eta_min"], "Expected numeric fact differs.")
            require(f"{reviewed['final_minute_wording']} minutes" in trial["transcript"], "Reviewed number phrase differs.")
            if case == "waypoint":
                require("zero extra minutes" in trial["transcript"], "Extra-minute phrase differs.")
        else:
            require(capture["status"] == "failed" and capture["failed_stage"] == "connect_and_session_ready", "Failure boundary differs.")
            require(not trial["declared_speech"] and not trial["transcript"], "Unexpected baseline speech.")
            require(not any(i.get("timing", {}).get("published_samples") for i in capture["inputs"]), "Baseline input was sent.")
            values = array("h", pcm)
            baseline_pcm = {"minimum_pcm16": min(values), "maximum_pcm16": max(values),
                            "nonzero_samples": sum(v != 0 for v in values),
                            "note": "Near-silent decoded PCM, not exactly zero-valued audio; no physical listening claim."}
        rows.append({"id": name, "case": case, "asr_status": trial["status"], "capture_status": capture["status"],
                     "verbatim_transcript": trial["transcript"], "exact_reviewed_transcript_guard": True,
                     "core_final_requested_facts_mentioned_with_disclosed_name_variants": facts_present,
                     "expected_final_facts": {f: facts_present for f in protocol["expected"][case]["result_facts"]},
                     "manual_review": {k: v for k, v in reviewed.items() if k != "transcript"},
                     "speech_handle_comparison": {"original_interruption_required": interruption_required,
                         "original_result_state": results[0]["state"] if results else None,
                         "required_original_interruption_observed": results[0]["state"] == "interruption_requested" if interruption_required else None,
                         "final_result_state": results[-1]["state"] if results else None,
                         "final_result_intent_version": results[-1]["intent_version"] if results else None},
                     "declared_speech_handles": trial["declared_speech"],
                     "received_duration_seconds": trial["received_duration_seconds"],
                     "resampled_duration_seconds": samples / 16000, "near_silent_baseline_pcm": baseline_pcm})
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "All four frozen whole-stream ASR outputs, including the readiness-failed baseline; final content and required handle states are separate from ASR completion.",
              "input_sha256": GUARDS, "analysis_script_sha256": digest(Path(__file__)),
              "review_method_timing": method["timing"],
              "counts": {"declared": 4, "asr_completed": 4,
                         "capture_status_counts": dict(Counter(r["capture_status"] for r in rows)),
                         "core_final_requested_facts_mentioned_with_name_variants": 3,
                         "final_expected_duration_mentioned": 3, "failed_baseline_without_content": 1,
                         "waypoint_final_13_and_zero_extra": 2, "waypoint_denominator": 2,
                         "required_original_result_interruptions": 3,
                         "required_original_result_interruptions_observed": sum(r["speech_handle_comparison"]["required_original_interruption_observed"] is True for r in rows),
                         "final_result_handles_finished": 3,
                         "rows_with_proper_name_deviations": 3, "raw_and_resampled_files_verified": len(hashes)},
              "trials": rows, "raw_and_resampled_sha256": hashes,
              "asr_report_receipts": {k: report[k] for k in ("inputs_unchanged", "source_unchanged", "suite_unchanged")},
              "limits": [
                  "ASR completed for 4/4 streams; final requested facts appear in 3/4 with disclosed name variants. Baseline's readiness timeout, absent input and empty transcript remain failures in the four-case denominator.",
                  "Waypoint1 uses the word 'thirteen'; waypoint2 uses digits '13'. Both retain 'zero extra minutes'; correction retains 'twenty-nine'. No false 13-to-30 substitution is present in these streams.",
                  "All 3/3 required original result handles record interruption_requested and only truncated old-result content is recognized. This is not a claim about physical acoustic stop latency.",
                  "The final place names have spelling deviations. Whole-stream ASR cannot distinguish synthesis pronunciation from recognizer errors or establish human intelligibility.",
                  "The final waypoint statement retains the declared MG Road destination; it does not literally say 'unchanged'. Backend state and actual effects are audited separately.",
                  "All streams are untrimmed with exact durations preserved after resampling. No retries, source changes or inference were performed by this review.",
                  "Four fixed synthetic development cases with warm shared services and observer instrumentation do not establish held-out accuracy, causal performance improvement or universal interruption reliability.",
              ]}
    with output.open("x") as handle:
        handle.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(output), "counts": result["counts"]}))


if __name__ == "__main__":
    main()
