"""Preserve all four received transcripts, including unmet navigation requests."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import wave

BASE = Path(__file__).resolve().parent
REVIEWED = {
    "rtc-cancel-1": {
        "transcript": "One moment. Navigation to Ok, one sec. Navigation to Kempegauda International Airport has been cancelled.",
        "facts": {"confirmed_airport_cancellation": True},
        "complete_declared_final_speech": True,
        "note": "Cancellation is confirmed. Cleared backend state is checked separately, not inferred solely from speech.",
    },
    "rtc-repeat-1": {
        "transcript": "One moment. Navigation to OK, one sec. Now navigate. Sure, let me check. The route to Kempegauda International Airport has been successfully computed with Route IDR three.",
        "facts": {"airport_navigation_active": False, "office_replaced": False, "arrival_88_minutes": False},
        "complete_declared_final_speech": False,
        "note": "The final reply truthfully reports computation only and exposes a route ID; it does not fulfill the navigation request. The final airport route was never activated.",
    },
    "rtc-repeat-2": {
        "transcript": "One moment. Navigation to OK, one sec. Now navigating. Sure, let me check. Navigation to Kempegauda International Airport has started. The route is active, with an estimated time of arrival of 88 minutes.",
        "facts": {"airport_navigation_active": True, "office_replaced": False, "arrival_88_minutes": True},
        "complete_declared_final_speech": False,
        "note": "Airport start and 88 minutes are present, but the predeclared replaced-office fact is absent from both generated and recognized final speech. Backend replacement succeeds separately.",
    },
    "rtc-cancel-2": {
        "transcript": "One moment. Navigation to OK, one sec. Navigation to Kempegauda International Airport has been cancelled.",
        "facts": {"confirmed_airport_cancellation": True},
        "complete_declared_final_speech": True,
        "note": "Cancellation is confirmed. Cleared backend state is checked separately, not inferred solely from speech.",
    },
}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def main():
    target = BASE / "received-audio-review/qualitative-review.json"
    assert not target.exists(), "Preserve existing review."
    report = json.loads((BASE / "received-audio-review/report.json").read_text())
    protocol = json.loads((BASE / "protocol.json").read_text())
    audit = json.loads((BASE / "audit.json").read_text())
    method = json.loads((BASE / "received-audio-review-protocol.json").read_text())
    assert [t["id"] for t in report["trials"]] == list(REVIEWED) == [t["id"] for t in method["declared_trials"]]
    hashes, rows = {}, []
    for trial in report["trials"]:
        name, case = trial["id"], trial["case"]
        reviewed = REVIEWED[name]
        assert trial["status"] == "completed" and trial["transcript"] == reviewed["transcript"]
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
        assert len(results) == len(protocol["expected"][case]["turns"])
        assert results[-1]["state"] == "finished" and all(h["state"] == "interruption_requested" for h in results[:-1])
        checked = next(t for t in audit["trials"] if t["name"] == name)
        rows.append({"id": name, "case": case, "asr_status": trial["status"],
                     "capture_status": capture["status"], "manual_review": reviewed,
                     "semantic_effect_case_passed": all(checked["semantic_checks"].values()),
                     "name_variants": {"Kempegowda": "Kempegauda"},
                     "other_variants": "Route ID R3 is recognized as Route IDR three in failed repeat1; acknowledgement spelling/punctuation and interrupted fragments differ.",
                     "declared_result_speech": results,
                     "received_duration_seconds": trial["received_duration_seconds"]})
    sources = [Path(__file__), BASE / "protocol.json", BASE / "audit.json", BASE / "received-audio-review-protocol.json",
               BASE / "received-audio-review/report.json", BASE / "received-audio-review/protocol.json"]
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Manual review of all frozen whole-stream local ASR outputs; no human listening or new inference.",
        "input_sha256": {str(p.relative_to(BASE)): digest(p) for p in sources},
        "raw_and_resampled_sha256": hashes, "trials": rows,
        "counts": {"declared": 4, "asr_completed": 4, "capture_completed": 4,
                   "semantic_effect_cases_passed": sum(r["semantic_effect_case_passed"] for r in rows),
                   "complete_declared_final_speech_facts": sum(r["manual_review"]["complete_declared_final_speech"] for r in rows),
                   "final_result_handles_finished": 4, "required_original_result_interruptions": 6,
                   "required_original_result_interruptions_observed": 6,
                   "correct_cancellation_replies": 2, "cancellation_denominator": 2,
                   "complete_return_replies": 0, "return_denominator": 2,
                   "rows_with_proper_name_deviations": 4, "raw_and_resampled_files_verified": len(hashes)},
        "asr_report_receipts": {k: report[k] for k in ("inputs_unchanged", "source_unchanged", "suite_unchanged")},
        "limits": ["All final speeches finish, including a reply that only reports computation; finished speech is not completed requested work.",
                   "The failed return stays in every relevant denominator; second return's missing office-replacement clause remains a spoken-content failure despite successful state change.",
                   "All initial/middle replies are intentionally interrupted, so their full wording is not required in whole-stream ASR.",
                   "Names remain verbatim; ASR cannot distinguish synthesis pronunciation from recognition errors or establish physical playback timing."]}
    with target.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result["counts"]))


if __name__ == "__main__":
    main()
