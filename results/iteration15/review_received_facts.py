"""Preserve all four received transcripts, including unmet navigation requests."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import wave

BASE = Path(__file__).resolve().parent
REVIEWED = {'rtc-cancel-1': {'transcript': 'One moment. Navigation to OK, one sec. Navigation to Kempegauda International '
                                'Airport has been cancelled.',
                  'facts': {'confirmed_airport_cancellation': True},
                  'complete_declared_final_speech': True,
                  'note': 'Confirmed airport cancellation; cleared backend state is independently checked.',
                  'name_variants': {'Kempegowda': 'Kempegauda'}},
 'rtc-repeat-1': {'transcript': 'One moment. Navigation to okay, one sec. Now navigating. Sure, let me check. Now '
                                'navigating to Kempegauda International Airport, replacing office, Maniata Tech '
                                'Park. Estimated arrival in eighty-eight minutes.',
                  'facts': {'airport_navigation_active': True,
                            'office_replaced': True,
                            'arrival_88_minutes': True},
                  'complete_declared_final_speech': True,
                  'note': 'Final speech reports airport navigation replacing the office and an '
                          'eighty-eight-minute estimate. Proper-name spelling differences are retained '
                          'separately; no human listening.',
                  'name_variants': {'Kempegowda': 'Kempegauda', 'Manyata': 'Maniata'}},
 'rtc-repeat-2': {'transcript': 'One moment Navigation to Okay, one sec Now navigating Sure, let me check '
                                'Navigation to Kempegauda International Airport has started. The route is active '
                                'with an estimated time of arrival of eighty-eight minutes.',
                  'facts': {'airport_navigation_active': True,
                            'office_replaced': False,
                            'arrival_88_minutes': True},
                  'complete_declared_final_speech': False,
                  'note': 'Airport activation and eighty-eight minutes are present; the predeclared '
                          'replaced-office fact is absent from both generated and recognized final speech. '
                          'Backend replacement succeeds separately.',
                  'name_variants': {'Kempegowda': 'Kempegauda'}},
 'rtc-cancel-2': {'transcript': 'One moment. Navigation. Okay, one sec. Navigation to Kempegauda International '
                                'Airport has been cancelled.',
                  'facts': {'confirmed_airport_cancellation': True},
                  'complete_declared_final_speech': True,
                  'note': 'Confirmed airport cancellation; cleared backend state is independently checked.',
                  'name_variants': {'Kempegowda': 'Kempegauda'}}}


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
                     "name_variants": reviewed["name_variants"],
                     "other_variants": "Eighty-eight is the correct spoken numeric value; original hyphenation, acknowledgement spelling/punctuation and interrupted fragments remain verbatim.",
                     "declared_result_speech": results,
                     "received_duration_seconds": trial["received_duration_seconds"]})
    sources = [Path(__file__).resolve(), BASE / "protocol.json", BASE / "audit.json", BASE / "received-audio-review-protocol.json",
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
                   "complete_return_replies": sum(r["case"] == "repeat" and r["manual_review"]["complete_declared_final_speech"] for r in rows), "return_denominator": 2,
                   "rows_with_proper_name_deviations": 4, "raw_and_resampled_files_verified": len(hashes)},
        "asr_report_receipts": {k: report[k] for k in ("inputs_unchanged", "source_unchanged", "suite_unchanged")},
        "limits": ["All final speeches finish; one omits a required replacement fact. Finished speech remains separate from completed requested work.",
                   "The second return stays a spoken-content failure for its missing office-replacement clause despite successful state change. The first return passes after a logged plan repair.",
                   "All initial/middle replies are intentionally interrupted, so their full wording is not required in whole-stream ASR.",
                   "Names remain verbatim; ASR cannot distinguish synthesis pronunciation from recognition errors or establish physical playback timing."]}
    with target.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result["counts"]))


if __name__ == "__main__":
    main()
