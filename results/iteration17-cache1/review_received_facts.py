"""Guarded manual review of this arm's four whole-stream ASR transcripts.

No inference or human listening. Earlier-result completion is preserved separately
from final facts and is never assumed to be an interruption.
"""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import wave

BASE = Path(__file__).resolve().parent
REVIEWED = {'rtc-cancel-1': {'transcript': 'One moment. Navigation to Kempegauda International Airport has started. '
                                'Estimated arrival in eighty-eight minutes. Okay, one sec. Navigation to '
                                'Kempegauda International Airport has been cancelled.',
                  'facts': {'confirmed_airport_cancellation': True},
                  'complete_declared_final_speech': True,
                  'name_variants': {'Kempegowda': ['Kempegauda']},
                  'note': 'Final generated and recognized reply confirms airport navigation cancellation; '
                          'cleared state is checked separately. The preceding initial airport result '
                          'finished, with its full88-minute confirmation recognized; the required '
                          'interruption failed and remains counted.',
                  'other_variants': 'Digit88 and written eighty-eight express the same required value. '
                                    'Original acknowledgment, punctuation and interrupted wording are '
                                    'preserved.'},
 'rtc-repeat-1': {'transcript': 'One moment Navigation to Okay, one sec Now navigating Sure, let me check '
                                'Now navigating to Kempegauda International Airport, replacing office '
                                'Maniata Tech Park, estimated arrival in eighty-eight minutes.',
                  'facts': {'airport_navigation_active': True,
                            'office_replaced': True,
                            'arrival_88_minutes': True},
                  'complete_declared_final_speech': True,
                  'name_variants': {'Kempegowda': ['Kempegauda'], 'Manyata': ['Maniata']},
                  'note': 'Final generated and recognized reply confirms airport navigation replacing the '
                          'office and an88-minute estimate. Earlier result fragments remain in the '
                          'whole-stream transcript; their full wording is not required.',
                  'other_variants': 'Digit88 and written eighty-eight express the same required value. '
                                    'Original acknowledgment, punctuation and interrupted wording are '
                                    'preserved.'},
 'rtc-repeat-2': {'transcript': 'One moment Navigation to Kempigauda International Airport has started. '
                                'Estimated arrival in 88 minutes. Okay, one sec. Now navigating to Office, '
                                'Maniata Tech Park, replacing Kempegauda International Sure, let me check. '
                                'Now navigating to Kempegauda International Airport, replacing Office, '
                                'Maniata Tech Park, estimated arrival in 88 minutes.',
                  'facts': {'airport_navigation_active': True,
                            'office_replaced': True,
                            'arrival_88_minutes': True},
                  'complete_declared_final_speech': True,
                  'name_variants': {'Kempegowda': ['Kempigauda', 'Kempegauda'], 'Manyata': ['Maniata']},
                  'note': 'Final generated and recognized reply confirms airport navigation replacing the '
                          'office and an88-minute estimate. The preceding initial airport result finished, '
                          'with its full88-minute confirmation recognized; the required interruption failed '
                          'and remains counted.',
                  'other_variants': 'Digit88 and written eighty-eight express the same required value. '
                                    'Original acknowledgment, punctuation and interrupted wording are '
                                    'preserved.'},
 'rtc-cancel-2': {'transcript': 'One moment Navigation to Kempegauda International Airport has started. '
                                'Estimated arrival in eighty-eight minutes. Okay, one sec. Navigation to '
                                'Kempegauda International Airport has been canceled.',
                  'facts': {'confirmed_airport_cancellation': True},
                  'complete_declared_final_speech': True,
                  'name_variants': {'Kempegowda': ['Kempegauda']},
                  'note': 'Final generated and recognized reply confirms airport navigation cancellation; '
                          'cleared state is checked separately. The preceding initial airport result '
                          'finished, with its full88-minute confirmation recognized; the required '
                          'interruption failed and remains counted.',
                  'other_variants': 'Digit88 and written eighty-eight express the same required value. '
                                    'Original acknowledgment, punctuation and interrupted wording are '
                                    'preserved. Recognized canceled differs in spelling from generated '
                                    'cancelled, without changing the cancellation fact.'}}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def main():
    output = BASE / "received-audio-review/qualitative-review.json"
    assert not output.exists(), "Preserve existing review."
    report = json.loads((BASE / "received-audio-review/report.json").read_text())
    protocol = json.loads((BASE / "protocol.json").read_text())
    method = json.loads((BASE / "received-audio-review-protocol.json").read_text())
    audit = json.loads((BASE / "audit.json").read_text())
    assert [t["id"] for t in report["trials"]] == list(REVIEWED) == [t["id"] for t in method["declared_trials"]]
    hashes, rows = {}, []
    for trial in report["trials"]:
        name, case = trial["id"], trial["case"]
        reviewed = REVIEWED[name]
        assert trial["status"] == "completed" and trial["transcript"] == reviewed["transcript"], "Changed output requires new manual review."
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
        assert pcm.stat().st_size % 2 == 0 and pcm.stat().st_size // 2 == trial["received_samples"]
        assert pcm.stat().st_size / 48000 == trial["resampled"]["samples"] / 16000 == trial["received_duration_seconds"]
        capture = json.loads((BASE / name / "report.json").read_text())
        assert capture["speech_handles"] == trial["declared_speech"]
        results = [h for h in trial["declared_speech"] if h["kind"] == "result"]
        by_version = {h["intent_version"]: h for h in results}
        final_version = len(protocol["expected"][case]["turns"])
        assert len(results) == len(by_version) == final_version
        assert set(by_version) == set(range(1, final_version + 1))
        checked = next(t for t in audit["trials"] if t["name"] == name)
        interruptions = []
        for old, new in protocol["expected"][case]["required_interruption_pairs"]:
            handle = by_version[old]
            observed = next(p for p in checked["interruptions"] if p["from_intent_version"] == old and p["to_intent_version"] == new)
            assert handle["state"] == observed["old_handle_state"]
            interruptions.append({"from_intent_version": old, "to_intent_version": new,
                                  "actual_old_handle_state": handle["state"],
                                  "required_interruption_observed": handle["state"] == "interruption_requested",
                                  "old_result_finished_instead": handle["state"] == "finished"})
        rows.append({"id": name, "case": case, "asr_status": trial["status"],
                     "capture_status": capture["status"], "manual_review": reviewed,
                     "semantic_effect_case_passed": bool(checked["semantic_checks"]) and all(v is True for v in checked["semantic_checks"].values()),
                     "full_trace_equality": checked["checks"]["full_trace_equality"],
                     "all_core_checks_passed": all(v is True for v in checked["checks"].values()),
                     "required_interruptions": interruptions,
                     "final_result_handle_finished": by_version[final_version]["state"] == "finished",
                     "name_variants": reviewed["name_variants"], "declared_result_speech": results,
                     "received_duration_seconds": trial["received_duration_seconds"]})
    pairs = [p for r in rows for p in r["required_interruptions"]]
    source_paths = [Path(__file__).resolve(), BASE / "protocol.json", BASE / "audit.json",
                    BASE / "received-audio-review-protocol.json", BASE / "received-audio-review/report.json",
                    BASE / "received-audio-review/protocol.json"]
    result = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Independent manual reading of all four exact whole-stream local ASR outputs and declared result handles. No human listening, inference, trimming or retries.",
              "input_sha256": {str(p.relative_to(BASE)): digest(p) for p in source_paths},
              "raw_and_resampled_sha256": hashes, "trials": rows,
              "counts": {"declared": len(REVIEWED), "asr_completed": sum(r["asr_status"] == "completed" for r in rows),
                         "capture_completed": sum(r["capture_status"] == "completed_observation" for r in rows),
                         "semantic_effect_cases_passed": sum(r["semantic_effect_case_passed"] for r in rows),
                         "complete_declared_final_speech_facts": sum(r["manual_review"]["complete_declared_final_speech"] for r in rows),
                         "final_result_handles_finished": sum(r["final_result_handle_finished"] for r in rows),
                         "required_original_result_interruptions": len(pairs),
                         "required_original_result_interruptions_observed": sum(p["required_interruption_observed"] for p in pairs),
                         "old_result_handles_finished_instead": sum(p["old_result_finished_instead"] for p in pairs),
                         "old_result_handle_state_counts": dict(Counter(p["actual_old_handle_state"] for p in pairs)),
                         "correct_cancellation_replies": sum(r["case"] == "cancel" and r["manual_review"]["complete_declared_final_speech"] for r in rows),
                         "cancellation_denominator": sum(r["case"] == "cancel" for r in rows),
                         "complete_return_replies": sum(r["case"] == "repeat" and r["manual_review"]["complete_declared_final_speech"] for r in rows),
                         "return_denominator": sum(r["case"] == "repeat" for r in rows),
                         "full_trace_equality_cases": sum(r["full_trace_equality"] for r in rows),
                         "all_core_checks_passed_cases": sum(r["all_core_checks_passed"] for r in rows),
                         "rows_with_proper_name_deviations": sum(bool(r["name_variants"]) for r in rows),
                         "raw_and_resampled_files_verified": len(hashes)},
              "preserved_core_failed_checks": audit["failed_checks"],
              "asr_report_receipts": {k: report[k] for k in ("inputs_unchanged", "source_unchanged", "suite_unchanged")},
              "limits": [
                  "Final speech facts, backend effects, required interruption and trusted trace equality have separate denominators. A complete final reply does not repair a failed interruption or attribution check.",
                  "The whole received stream is retained. Initial/middle replies were intended to be interrupted; actual finished earlier replies remain finished and their full recognized text is not dropped.",
                  "Names, number spelling, cancellation spelling, punctuation and interrupted fragments remain verbatim. ASR cannot distinguish synthesis pronunciation from recognition errors or establish human intelligibility.",
                  "Cancellation criterion requires confirmation of actual airport cancellation; cleared backend state is separately checked. Return criterion requires airport, office replacement and88-minute estimate.",
                  "These reused synthetic fixed-order cases are a development screen, not general accuracy or independent hardware replicates. Speech-handle finish is not physical audible completion."]}
    with output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps(result["counts"], indent=2))


if __name__ == "__main__":
    main()
