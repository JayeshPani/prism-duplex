"""Guarded manual review of all four iteration11 whole-stream ASR observations.

No inference, audio listening, trimming, retries, or source/raw-artifact edits.
Changed evidence requires a new manual review before reusing these annotations.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import wave

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
GUARDS = {
    "protocol.json": "cad4a9e67376042b4f858839ec05291eae64e18dd622f7450fb81d41f5904d61",
    "received-audio-review-protocol.json": "f6e9e50407eed71472826ceb2a4897665472ca46b76ae2cea51467c71dcf876e",
    "received-audio-review/protocol.json": "f556053c547c3a5f05b1b00b69bed21ca25d76df8237b3138f62836fc9610ade",
    "received-audio-review/report.json": "b81180b02303ca49bce2b0adcdeb97d0a4d01bf25e1e705846fc782e672a5fd8",
}
REVIEWED = {
    "rtc-waypoint-1": {
        "transcript": "One moment Navigation Okay, one sec Added blue talkai in Duriniger as a stop. Estimated time to MG Road Metro Station is thirteen minutes, zero extra minutes.",
        "final_minute_wording": "thirteen", "final_minutes": 13, "extra_minute_wording": "zero", "extra_minutes": 0,
        "proper_name_deviations": {"Blue Tokai": "blue talkai", "Indiranagar": "Duriniger"},
        "initial_result_coverage": "Only 'Navigation' remains; initial destination, started clause and initial estimate are absent, consistent with the recorded interruption request.",
        "initial_result_state": "interruption_requested",
        "other_wording_deviations": ["Punctuation between acknowledgment and truncated navigation clauses is absent."],
    },
    "rtc-baseline-1": {
        "transcript": "One moment. Navigation to Kempegauda International Airport has started. Estimated arrival in 88 minutes.",
        "final_minute_wording": "88", "final_minutes": 88, "extra_minute_wording": None, "extra_minutes": None,
        "proper_name_deviations": {"Kempegowda": "Kempegauda"},
        "initial_result_coverage": "Full airport-start relation and 88-minute estimate are represented.",
        "initial_result_state": "finished", "other_wording_deviations": [],
    },
    "rtc-waypoint-2": {
        "transcript": "One moment Navigation MG Road Metro Station has started. Estimated arrival in thirteen minutes. Okay, one sec. Added blue talkai in Durinegar as a stop. Estimated time to MG Road Metro Station is thirteen minutes, zero extra minutes.",
        "final_minute_wording": "thirteen", "final_minutes": 13, "extra_minute_wording": "zero", "extra_minutes": 0,
        "proper_name_deviations": {"Blue Tokai": "blue talkai", "Indiranagar": "Durinegar"},
        "initial_result_coverage": "Full initial MG Road start and thirteen-minute estimate remain. This does not meet the declared original-result interruption requirement.",
        "initial_result_state": "finished",
        "other_wording_deviations": ["The preposition 'to' is absent after the initial 'Navigation'.", "Punctuation after the first acknowledgment is absent."],
    },
    "rtc-correction-1": {
        "transcript": "One moment Navigation to Ok, one sec. Now navigating to office, Maniata Tech Park, replacing Kempegauda International Airport, estimated arrival in twenty-nine minutes.",
        "final_minute_wording": "twenty-nine", "final_minutes": 29, "extra_minute_wording": None, "extra_minutes": None,
        "proper_name_deviations": {"Manyata": "Maniata", "Kempegowda": "Kempegauda"},
        "initial_result_coverage": "Only 'Navigation to' remains from the old result. Its initial airport-start and 88-minute estimate are absent; the final replacement clause names the airport separately.",
        "initial_result_state": "interruption_requested",
        "other_wording_deviations": ["Acknowledgment 'Okay' is rendered 'Ok'; punctuation boundaries differ."],
    },
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=BASE / "received-audio-review/qualitative-review.json")
    args = parser.parse_args()
    require(not args.out.exists(), "refusing existing review")
    for name, digest in GUARDS.items():
        require(sha(BASE / name) == digest, f"changed evidence needs new manual review: {name}")
    report = json.loads((BASE / "received-audio-review/report.json").read_text())
    protocol = json.loads((BASE / "protocol.json").read_text())
    method = json.loads((BASE / "received-audio-review-protocol.json").read_text())
    require([x["id"] for x in report["trials"]] == list(REVIEWED) == [x["id"] for x in method["declared_trials"]], "declared trial order differs")
    reviewed, hashes = [], []
    for trial in report["trials"]:
        name, case = trial["id"], trial["case"]
        annotation = REVIEWED[name]
        require(trial["transcript"] == annotation["transcript"], f"new transcript review needed: {name}")
        require(trial["status"] == "completed", f"incomplete ASR row: {name}")
        for relative, expected in trial["input_sha256"].items():
            path = BASE / name / relative
            observed = sha(path)
            require(observed == expected, f"changed raw artifact: {path}")
            hashes.append({"file": str(path.relative_to(ROOT)), "sha256": observed, "matches": True})
        wav = BASE / "received-audio-review" / trial["resampled"]["file"]
        require(sha(wav) == trial["resampled"]["sha256"], "resampled WAV differs")
        hashes.append({"file": str(wav.relative_to(ROOT)), "sha256": sha(wav), "matches": True})
        pcm_bytes = (BASE / name / trial["pcm_file"]).stat().st_size
        with wave.open(str(wav), "rb") as f:
            require((f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, 16000), "resampled format differs")
            samples = f.getnframes()
        require(pcm_bytes % 2 == 0 and pcm_bytes // 2 == trial["received_samples"], "received sample count differs")
        require(samples == trial["resampled"]["samples"], "resampled sample count differs")
        require(pcm_bytes / 48000 == samples / 16000 == trial["received_duration_seconds"] == trial["resampled"]["duration_seconds"], "whole-stream duration differs")
        capture = json.loads((BASE / name / "report.json").read_text())
        require(capture["speech_handles"] == trial["declared_speech"], "speech-handle copy differs")
        results = [h for h in trial["declared_speech"] if h["kind"] == "result"]
        require(results[0]["state"] == annotation["initial_result_state"], "changed handle requires review")
        require(results[-1]["state"] == "finished", "final result not finished")
        require(f"{annotation['final_minute_wording']} minutes" in trial["transcript"], "reviewed duration phrase differs")
        final_expected = protocol["expected"][case]["effects"][-1]["eta_min"]
        require(annotation["final_minutes"] == final_expected, "reviewed expected duration differs")
        needs_interruption = bool(protocol["expected"][case].get("followup_audio"))
        interruption = results[0]["state"] == "interruption_requested" if needs_interruption else None
        reviewed.append({
            "id": name, "case": case, "asr_status": trial["status"], "capture_status": trial["capture_status"],
            "verbatim_transcript": trial["transcript"], "exact_reviewed_transcript_guard": True,
            "expected_final_facts": {fact: True for fact in protocol["expected"][case]["result_facts"]},
            "core_final_requested_facts_mentioned_with_disclosed_name_variants": True,
            "fact_scope": "Final requested action relation, destination and duration clauses are represented with the named spelling deviations. 'Unchanged' destination is compared to the declared initial destination; tool state is verified in the separate raw audit.",
            "manual_review": {key: value for key, value in annotation.items() if key != "transcript"},
            "speech_handle_comparison": {
                "original_interruption_required": needs_interruption,
                "original_result_state": results[0]["state"],
                "required_original_interruption_observed": interruption,
                "final_result_state": results[-1]["state"], "final_result_intent_version": results[-1]["intent_version"],
                "content_and_required_handle_state_met": not needs_interruption or interruption,
            },
            "declared_speech_handles": trial["declared_speech"],
            "received_duration_seconds": trial["received_duration_seconds"], "resampled_duration_seconds": samples / 16000,
        })
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "All four received whole-stream ASR outputs reviewed against frozen final facts, with original/final speech-handle states assessed separately; no general quality score.",
        "input_sha256": GUARDS, "analysis_script_sha256": sha(Path(__file__)),
        "review_method_timing": method["timing"],
        "counts": {"declared": 4, "asr_completed": 4,
                   "capture_status_counts": dict(Counter(x["capture_status"] for x in reviewed)),
                   "core_final_requested_facts_mentioned_with_name_variants": 4,
                   "final_expected_duration_mentioned": 4,
                   "waypoint_final_thirteen_and_zero_extra": 2, "waypoint_denominator": 2,
                   "required_original_result_interruptions": 3,
                   "required_original_result_interruptions_observed": sum(x["speech_handle_comparison"]["required_original_interruption_observed"] is True for x in reviewed),
                   "content_and_required_handle_state_met": sum(x["speech_handle_comparison"]["content_and_required_handle_state_met"] for x in reviewed),
                   "rows_with_proper_name_deviations": 4, "raw_and_resampled_files_verified": len(hashes)},
        "trials": reviewed, "raw_and_resampled_hash_checks": hashes,
        "asr_report_receipts": {key: report[key] for key in ("inputs_unchanged", "source_unchanged", "suite_unchanged")},
        "limits": [
            "Final requested content is represented in 4/4 streams with proper-name deviations. This is not a 4/4 full-scenario or exact-name fidelity pass.",
            "Original-result interruption is recorded for waypoint1 and correction, 2/3 required cases. Waypoint2's old result finished and its complete content remains; that declared interruption requirement fails.",
            "Both final stop summaries say 'thirteen' and 'zero'; the office replacement says 'twenty-nine'. Verbatim strings remain available and are not normalized to erase numeric differences.",
            "Truncated original results in waypoint1/correction are consistent with intended interruption, not missing required final response facts. A finished speech handle is distinct from physical acoustic playback.",
            "Whole-stream ASR does not provide word timing or prove human intelligibility. This review cannot isolate pronunciation versus recognition as the cause of name variations.",
            "All four complete streams remain untrimmed. Four fixed synthetic development captures share warm models and candidate instrumentation; no held-out accuracy or causal historical before/after latency claim.",
            "The ASR method file was frozen after RTC launch but before received ASR; the suite had already declared cases, whole streams and expected facts. The preparation timing exception is preserved.",
            "Full raw transport/effect, interruption timing, source provenance and cleanup audits remain separate; this review makes no new inference or production change.",
        ],
    }
    with args.out.open("x") as f:
        f.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.out), "counts": result["counts"]}, indent=2))


if __name__ == "__main__":
    main()
