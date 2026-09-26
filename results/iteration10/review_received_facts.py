"""Guarded manual review of four complete, untrimmed received-audio ASR rows.

Read-only derivation: no model imports, inference, audio listening or raw edits.
Changed inputs/transcripts require new manual review; existing output is refused.
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
    "protocol.json": "24047888b19c9490c78aa09dfbd5a188c5163b031edb9b963e9150be45683429",
    "received-audio-review-protocol.json": "86c508ea7d702392fce8624e4b6782ac0ccdab16abda65dcfb52634bf28499e8",
    "received-audio-review/report.json": "382cde936b836e0ab1efc189609bc8cd6f68197a4cb8016372e6e53b807c7207",
    "received-audio-review/protocol.json": "dd510041a3505fa8c002193e76ed1c79c0c94967e4fff03d4b1e62f94fea8aab",
}
TRANSCRIPTS = {
    "rtc-waypoint-1": "One moment. Navigation MG Road Metro Station has started. Estimated arrival in 13 minutes. Okay, one sec. Added blue talkai in Durinigar as a stop. Estimated time to MG Road Metro Station is 13 minutes, zero extra minutes.",
    "rtc-baseline-1": "One moment. Navigation to Kempegauda International Airport has started. Estimated arrival in 88 minutes.",
    "rtc-waypoint-2": "Navigation to MG Road Metro Station has started. Estimated arrival in 13 minutes. Okay, one sec. Added blue talkai in Durinigar as a stop. Estimated time to MG Road Metro Station is 13 minutes, zero extra minutes.",
    "rtc-correction-1": "Navigation to Kempegauda International Airport has started. Estimated arrival in eighty-eight minutes.",
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
    require(not args.out.exists(), "refusing existing qualitative review")
    for name, digest in GUARDS.items():
        require(sha(BASE / name) == digest, f"changed input requires new manual review: {name}")
    report = json.loads((BASE / "received-audio-review/report.json").read_text())
    protocol = json.loads((BASE / "protocol.json").read_text())
    method = json.loads((BASE / "received-audio-review-protocol.json").read_text())
    require([r["id"] for r in report["trials"]] == list(TRANSCRIPTS), "missing or reordered declared trial")
    require([r["id"] for r in report["trials"]] == [r["id"] for r in method["declared_trials"]], "declaration differs")
    reviewed, hashes = [], []
    for trial in report["trials"]:
        name, case, text = trial["id"], trial["case"], trial["transcript"]
        require(text == TRANSCRIPTS[name], f"new transcript review required: {name}")
        require(trial["status"] == "completed", f"ASR row incomplete: {name}")
        for source_name, digest in trial["input_sha256"].items():
            path = BASE / name / source_name
            observed = sha(path)
            require(observed == digest, f"raw input changed: {path}")
            hashes.append({"file": str(path.relative_to(ROOT)), "sha256": observed, "matches": True})
        wav = BASE / "received-audio-review" / trial["resampled"]["file"]
        require(sha(wav) == trial["resampled"]["sha256"], "resampled WAV changed")
        hashes.append({"file": str(wav.relative_to(ROOT)), "sha256": sha(wav), "matches": True})
        pcm_samples = (BASE / name / trial["pcm_file"]).stat().st_size // 2
        with wave.open(str(wav), "rb") as f:
            require((f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, 16000), "WAV format differs")
            wav_samples = f.getnframes()
        require(pcm_samples == trial["received_samples"] and wav_samples == trial["resampled"]["samples"], "sample count differs")
        require(pcm_samples / 24000 == wav_samples / 16000 == trial["received_duration_seconds"], "whole-stream duration differs")
        capture = json.loads((BASE / name / "report.json").read_text())
        require(capture["speech_handles"] == trial["declared_speech"], "speech handle copy differs")
        handles = trial["declared_speech"]
        old_result = next(h for h in handles if h["intent_version"] == 1 and h["kind"] == "result")
        final_results = [h for h in handles if h["kind"] == "result" and h["intent_version"] > 1]
        needs_interruption = bool(protocol["expected"][case].get("followup_audio"))
        facts = {fact: case != "correction" for fact in protocol["expected"][case]["result_facts"]}
        if case == "waypoint":
            numbers = {"initial_estimate_minutes": 13, "final_estimate_minutes": 13, "final_extra_minutes": 0}
            names = {"Blue Tokai": "blue talkai", "Indiranagar": "Durinigar"}
            note = "The addition clause, MG Road destination, final 13 minutes and zero extra minutes are present; stop names use the recorded variant spellings. The complete initial navigation result also remains in ASR."
        elif case == "baseline":
            numbers = {"initial_estimate_minutes": 88, "final_estimate_minutes": 88, "final_extra_minutes": None}
            names = {"Kempegowda": "Kempegauda"}
            note = "Airport navigation started and the 88-minute estimate are present with the airport-name spelling deviation."
        else:
            numbers = {"initial_estimate_minutes": 88, "final_estimate_minutes": None, "final_extra_minutes": None}
            names = {"Kempegowda": "Kempegauda"}
            note = "Only the original airport result and eighty-eight minutes are present. Office/Manyata Tech Park, replacing the airport, and the requested 29-minute estimate are absent. This is a failed requested scenario, despite completed ASR of the available stream."
        interruption_met = old_result["state"] == "interruption_requested" if needs_interruption else None
        reviewed.append({
            "id": name, "case": case, "asr_status": trial["status"], "capture_status": trial["capture_status"],
            "capture_failure": {"stage": capture.get("failed_stage"), "error": capture.get("error")},
            "verbatim_transcript": text, "exact_reviewed_transcript_guard": True,
            "expected_final_requested_facts": facts,
            "core_final_requested_facts_mentioned_with_disclosed_name_variants": all(facts.values()),
            "manual_observed_numbers": numbers, "proper_name_deviations": names, "manual_review": note,
            "other_text_deviations": ["The preposition 'to' is absent after the first 'Navigation'."] if name == "rtc-waypoint-1" else [],
            "acknowledgment_observation": {"one_moment_mentioned": "One moment." in text,
                                          "okay_one_sec_mentioned": "Okay, one sec." in text,
                                          "limit": "Whole-stream text does not attribute acknowledgments to a particular speech handle."},
            "declared_speech_handles": handles,
            "scenario_boundary": {
                "original_result_interruption_required": needs_interruption,
                "original_result_recorded_state": old_result["state"],
                "required_original_result_interruption_observed": interruption_met,
                "final_result_intent_versions": [h["intent_version"] for h in final_results],
                "final_result_states": [h["state"] for h in final_results],
                "declared_content_and_handle_requirements_met": all(facts.values()) and (not needs_interruption or interruption_met),
                "full_scenario_claim": "Fails the required original-result interruption; content preservation does not rescue the scenario." if needs_interruption else "Content and handle requirements represented; tool effects, trace completeness and cleanup remain separate audits.",
            },
            "received_duration_seconds": trial["received_duration_seconds"], "resampled_duration_seconds": wav_samples / 16000,
        })
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Manual review of all four whole-stream ASR transcripts against the frozen requested facts, with speech-handle comparisons; no listening or inference.",
        "input_sha256": GUARDS, "analysis_script_sha256": sha(Path(__file__)), "raw_and_resampled_hash_checks": hashes,
        "counts": {
            "declared": 4, "asr_completed": sum(r["asr_status"] == "completed" for r in reviewed),
            "capture_status_counts": dict(Counter(r["capture_status"] for r in reviewed)),
            "core_final_requested_facts_mentioned_with_name_variants": sum(r["core_final_requested_facts_mentioned_with_disclosed_name_variants"] for r in reviewed),
            "waypoint_final_13_and_zero_extra": sum(r["case"] == "waypoint" and r["manual_observed_numbers"]["final_estimate_minutes"] == 13 and r["manual_observed_numbers"]["final_extra_minutes"] == 0 for r in reviewed),
            "waypoint_denominator": 2, "required_original_result_interruptions": 3,
            "required_original_result_interruptions_observed": sum(r["scenario_boundary"]["required_original_result_interruption_observed"] is True for r in reviewed),
            "declared_content_and_handle_requirements_met": sum(r["scenario_boundary"]["declared_content_and_handle_requirements_met"] for r in reviewed),
            "rows_with_proper_name_deviations": sum(bool(r["proper_name_deviations"]) for r in reviewed),
            "raw_or_resampled_files_verified": len(hashes),
        },
        "trials": reviewed,
        "asr_report_receipts": {k: report[k] for k in ("inputs_unchanged", "source_unchanged", "suite_unchanged")},
        "limits": [
            "Three of four final requested content sets are represented with disclosed name spelling deviations; this is not a 3/4 full-scenario pass or an exact entity-fidelity score.",
            "All three scenarios requiring old-result interruption recorded that original result as finished, not interruption_requested. Interrupted acknowledgment handles do not establish interruption of the original result.",
            "The second waypoint result belongs to intent version3; do not silently attribute it to version2 or infer that two published clips produced exactly two recognized intents.",
            "The failed correction capture remains in every declared denominator. Its original 88-minute airport result cannot count as the requested office replacement or 29-minute result.",
            "Whole-stream ASR is a proxy; it cannot establish human intelligibility, word timing, physical acoustic stop or whether pronunciation versus recognition caused a difference.",
            "No audio was trimmed, regenerated or retranscribed by this audit. Speech-handle states describe recorded lifecycle observations, not physical playback.",
            "Four fixed synthetic development captures share warm models; there is no held-out, population-accuracy or controlled RTC before/after claim.",
            "Source hashes and exact transcripts guard these manual labels. Future changed observations require fresh review. Raw transport/effect and latency audits remain separate.",
        ],
    }
    with args.out.open("x") as f:
        f.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"out": str(args.out), "counts": result["counts"]}, indent=2))


if __name__ == "__main__":
    main()
