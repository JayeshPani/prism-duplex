"""Review frozen whole-stream ASR against declared facts; no new inference."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import wave

BASE = Path(__file__).resolve().parent
REVIEWED = {
    "rtc-waypoint-1": {
        "transcript": "One moment Navigation Okay, one sec Added blue Takeai in Durinegar as a stop. Estimated time to MG Road Metro Station is thirteen minutes, zero extra minutes.",
        "number_phrase": "thirteen minutes, zero extra minutes",
        "name_variants": {"Blue Tokai": "blue Takeai", "Indiranagar": "Durinegar"},
        "original_reply": "Only Navigation remains from the intentionally interrupted first result.",
    },
    "rtc-baseline-1": {
        "transcript": "One moment Navigation to Kempegauda International Airport has started. Estimated arrival in 88 minutes.",
        "number_phrase": "88 minutes",
        "name_variants": {"Kempegowda": "Kempegauda"},
        "original_reply": "Complete airport start and 88-minute estimate are present.",
    },
    "rtc-waypoint-2": {
        "transcript": "One moment. Navigation. Okay, one sec. Added blue Takeai in Durinegar as a stop. Estimated time to MG Road Metro Station is thirteen minutes, zero extra minutes.",
        "number_phrase": "thirteen minutes, zero extra minutes",
        "name_variants": {"Blue Tokai": "blue Takeai", "Indiranagar": "Durinegar"},
        "original_reply": "Only Navigation remains from the intentionally interrupted first result.",
    },
    "rtc-correction-1": {
        "transcript": "One moment. Navigation to OK, one sec. Now navigating to office, Maniata Tech Park, replacing Kempegauda International Airport. Estimated arrival in 29 minutes.",
        "number_phrase": "29 minutes",
        "name_variants": {"Manyata": "Maniata", "Kempegowda": "Kempegauda"},
        "original_reply": "Only Navigation to remains from the old result; the final replacement clause separately names the airport.",
    },
}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def main():
    target = BASE / "received-audio-review/qualitative-review.json"
    assert not target.exists(), "Preserve existing review."
    report = json.loads((BASE / "received-audio-review/report.json").read_text())
    protocol = json.loads((BASE / "protocol.json").read_text())
    method = json.loads((BASE / "received-audio-review-protocol.json").read_text())
    assert [t["id"] for t in report["trials"]] == list(REVIEWED) == [t["id"] for t in method["declared_trials"]]
    hashes, rows = {}, []
    for trial in report["trials"]:
        name, case = trial["id"], trial["case"]
        reviewed = REVIEWED[name]
        assert trial["status"] == "completed" and trial["transcript"] == reviewed["transcript"]
        assert reviewed["number_phrase"] in trial["transcript"]
        for relative, expected in trial["input_sha256"].items():
            path = BASE / name / relative
            assert digest(path) == expected
            hashes[str(path.relative_to(BASE))] = expected
        path = BASE / "received-audio-review" / trial["resampled"]["file"]
        assert digest(path) == trial["resampled"]["sha256"]
        hashes[str(path.relative_to(BASE))] = digest(path)
        with wave.open(str(path), "rb") as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            assert wav.getnframes() == trial["resampled"]["samples"]
        pcm = BASE / name / trial["pcm_file"]
        assert pcm.stat().st_size // 2 == trial["received_samples"] and pcm.stat().st_size % 2 == 0
        assert pcm.stat().st_size / 48000 == trial["resampled"]["samples"] / 16000 == trial["received_duration_seconds"]
        capture = json.loads((BASE / name / "report.json").read_text())
        assert capture["speech_handles"] == trial["declared_speech"]
        results = [h for h in trial["declared_speech"] if h["kind"] == "result"]
        followup = bool(protocol["expected"][case].get("followup_audio"))
        assert len(results) == (2 if followup else 1)
        assert results[-1]["state"] == "finished"
        assert not followup or results[0]["state"] == "interruption_requested"
        rows.append({"id": name, "case": case, "asr_status": trial["status"],
                     "capture_status": capture["status"], "manual_review": reviewed,
                     "core_final_requested_facts_mentioned_with_disclosed_name_variants": True,
                     "expected_final_facts": {fact: True for fact in protocol["expected"][case]["result_facts"]},
                     "original_interruption_required": followup,
                     "original_result_state": results[0]["state"], "final_result_state": results[-1]["state"],
                     "received_duration_seconds": trial["received_duration_seconds"]})
    sources = [Path(__file__), BASE / "protocol.json", BASE / "received-audio-review-protocol.json",
               BASE / "received-audio-review/report.json", BASE / "received-audio-review/protocol.json"]
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Manual content review of all four frozen whole-stream local ASR transcripts; no human listening.",
        "input_sha256": {str(p.relative_to(BASE)): digest(p) for p in sources},
        "raw_and_resampled_sha256": hashes, "trials": rows,
        "counts": {"declared": 4, "asr_completed": 4, "capture_completed": 4,
                   "core_final_requested_facts_mentioned_with_name_variants": 4,
                   "final_result_handles_finished": 4, "required_original_result_interruptions": 3,
                   "required_original_result_interruptions_observed": 3,
                   "waypoint_final_13_and_zero_extra": 2, "waypoint_denominator": 2,
                   "rows_with_proper_name_deviations": 4, "raw_and_resampled_files_verified": len(hashes)},
        "asr_report_receipts": {k: report[k] for k in ("inputs_unchanged", "source_unchanged", "suite_unchanged")},
        "limits": ["Name variants and truncated old replies are retained verbatim, not normalized away.",
                   "The waypoint reply names MG Road but does not literally say unchanged; backend effects are separately audited.",
                   "ASR cannot distinguish pronunciation from recognition error, establish human intelligibility or word/physical playback timing.",
                   "Four reused fixed-order synthetic development cases do not establish causal speedup or universal reliability."]}
    with target.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result["counts"]))


if __name__ == "__main__":
    main()
