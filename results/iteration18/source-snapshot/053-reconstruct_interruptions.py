"""Offline receipt reconstruction only; does not import any model or RTC runtime."""
from pathlib import Path
import datetime
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).with_name("interruption-evidence-review.json")
ARMS = [("A2_first", "iteration17-cache2-before"), ("B1", "iteration17-cache1"),
        ("A2_return", "iteration17-cache2-after")]
FOCUS = {("B1", "rtc-cancel-1", "followup_1"),
         ("B1", "rtc-repeat-2", "followup_1"),
         ("B1", "rtc-repeat-2", "followup_2"),
         ("B1", "rtc-cancel-2", "followup_1"),
         ("A2_return", "rtc-cancel-1", "followup_1")}
HASHES, CHECKS = {}, {}


def read(path):
    data = path.read_bytes()
    HASHES[str(path.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
    return data


def load(path):
    return json.loads(read(path))


def rows(path):
    return [(i, json.loads(s)) for i, s in enumerate(read(path).splitlines(), 1)]


def ref(path, line=None, pointer=None):
    result = {"path": str(path.relative_to(ROOT))}
    if line is not None:
        result["line"] = line
    if pointer is not None:
        result["json_pointer"] = pointer
    return result


def ms(t, onset):
    return round((t - onset) * 1000, 6)


def frame_ref(path, item, onset):
    line, row = item
    return {**ref(path, line), **row, "after_input_onset_ms": ms(row["received_monotonic"], onset)}


def main():
    if OUT.exists():
        raise FileExistsError(OUT)
    pairs, logs, arm_health = [], {}, {}
    for arm, directory in ARMS:
        base = ROOT / "results" / directory
        stt_path = base / "local-stack-run1/stt-diagnostics/report.json"
        stt = load(stt_path)
        aec_path = base / "local-stack-run1/aec-discard-diagnostics.json"
        aec = load(aec_path)
        audit = load(base / "audit.json")
        arm_health[arm] = {key: stt[key] for key in (
            "dropped_calls", "dropped_requests", "observation_error_count", "pending_native_call_ids")}
        log_path = base / "local-stack-run1/worker.log"
        log_rows = []
        for i, line in enumerate(read(log_path).decode().splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                row = {"message": line, "format": "plain_text"}
            log_rows.append((i, row))
        logs[arm] = {"source": ref(log_path), "explicit_pause_resume_false_interruption_matches": [
            {**ref(log_path, i), "row": row} for i, row in log_rows
            if re.search(r"false.?interrupt|\bresume[ds]?\b|\bpaus(?:e[ds]?|ing)\b", row.get("message", ""), re.I)],
            "excluded_resuming_job_metadata": sum("resuming" in row for _, row in log_rows)}
        for name in ("rtc-cancel-1", "rtc-repeat-1", "rtc-repeat-2", "rtc-cancel-2"):
            folder = base / name
            report_path, event_path, frame_path = (folder / n for n in ("report.json", "events.jsonl", "frames.jsonl"))
            report, events, frames = load(report_path), rows(event_path), rows(frame_path)
            trace_path = base / "local-stack-run1/traces" / (report["room"] + ".jsonl")
            server = rows(trace_path)
            server_index = {json.dumps(row, sort_keys=True): i for i, row in server}
            for file, digest in report["artifact_sha256"].items():
                path = folder / file
                CHECKS[str(path.relative_to(ROOT)) + ":report_hash"] = hashlib.sha256(read(path)).hexdigest() == digest
            calls = [(i, c) for i, c in enumerate(stt["calls"]) if c["room"] == report["room"]]
            CHECKS[arm + "/" + name + ":one_recognition_per_input"] = len(calls) == len(report["inputs"])
            for turn_index, clip in enumerate(report["inputs"]):
                if turn_index == 0:
                    continue
                label = clip["label"]
                onset = clip["timing"]["started_monotonic"] + clip["activity_start_sample"] / clip["sample_rate"]
                speech_end = clip["timing"]["started_monotonic"] + clip["activity_end_sample"] / clip["sample_rate"]
                old = report[label + "_trigger"]
                old = next(h for h in report["speech_handles"] if h["speech_id"] == old["speech_id"])
                next_handle = min((h for h in report["speech_handles"] if h.get("intent_version") == turn_index + 1), key=lambda h: h["queued_at"])
                cutoff = next_handle["queued_at"]
                timeline = []
                for line, receipt in events:
                    event = receipt.get("event", {})
                    if event.get("type") not in {"user_state", "agent_state", "user_final", "speech_handle", "agent_say", "user_partial"}:
                        continue
                    if not old["queued_at"] <= receipt["received_monotonic"] <= cutoff:
                        continue
                    canonical = {k: event[k] for k in ("type", "data", "ts")}
                    server_line = server_index.get(json.dumps(canonical, sort_keys=True))
                    CHECKS[f"{arm}/{name}:{line}:server_match"] = server_line is not None
                    timeline.append({**ref(event_path, line), **canonical,
                                     "received_monotonic": receipt["received_monotonic"],
                                     "received_unix": receipt["received_unix"],
                                     "server_ref": ref(trace_path, server_line),
                                     "after_input_onset_ms": ms(receipt["received_monotonic"], onset),
                                     "server_to_receipt_ms": round((receipt["received_unix"] - event["ts"]) * 1000, 6)})
                user = next(x for x in timeline if x["type"] == "user_state" and x["data"]["state"] == "speaking" and x["after_input_onset_ms"] >= 0)
                final = next(x for x in timeline if x["type"] == "user_final" and x["data"]["intent_version"] == turn_index + 1)
                listening = next(x for x in timeline if x["type"] == "user_state" and x["data"]["state"] == "listening" and x["received_monotonic"] > user["received_monotonic"])
                states = [x for x in timeline if x["type"] == "agent_state" and onset <= x["received_monotonic"] <= old["ended_at"]]
                pause = next((x for x in states if x["data"]["state"] == "listening"), None)
                resume = next((x for x in states if x["data"]["state"] == "speaking" and pause and x["received_monotonic"] > pause["received_monotonic"]), None)
                loud = [(i, f) for i, f in frames if old["queued_at"] <= f["received_monotonic"] < cutoff and f["rms_db"] >= report["config"]["threshold_db"]]
                tagged = [(i, f) for i, f in loud if f.get("heuristic_result_speech_id") == old["speech_id"]]
                gaps = []
                for left, right in zip(loud, loud[1:]):
                    start, end = left[1]["received_monotonic"], right[1]["received_monotonic"]
                    if end <= onset or end - start < .3:
                        continue
                    between = [(i, f) for i, f in frames if start < f["received_monotonic"] < end]
                    gaps.append({"left_loud": frame_ref(frame_path, left, onset), "right_loud": frame_ref(frame_path, right, onset),
                                 "arrival_gap_ms": ms(end, start), "intervening_received_frames": len(between),
                                 "intervening_pcm_duration_ms": sum(f["samples"] / 24000 * 1000 for _, f in between),
                                 "intervening_max_rms_db": max((f["rms_db"] for _, f in between), default=None),
                                 "max_adjacent_frame_arrival_gap_ms": max((ms(y[1]["received_monotonic"], x[1]["received_monotonic"]) for x,y in zip([left]+between, between+[right])), default=None)})
                ci, call = calls[turn_index]
                call_ref = ref(stt_path, pointer=f"/calls/{ci}")
                boundaries = {k: {**v, "after_input_onset_ms": ms(v["monotonic_ns"] / 1e9, onset)} for k, v in call.items() if isinstance(v, dict) and "monotonic_ns" in v}
                stages = [{"name": s["name"], "status": s["status"], "wall_ms": s["wall_seconds"] * 1000,
                           "start_after_input_onset_ms": ms(s["start"]["monotonic_ns"] / 1e9, onset),
                           "end_after_input_onset_ms": ms(s["end"]["monotonic_ns"] / 1e9, onset)} for s in call["stages"]]
                requests = [{**ref(stt_path, pointer=f"/requests/{i}"), **r} for i, r in enumerate(stt["requests"]) if r["room"] == report["room"] and onset <= r["entry"]["monotonic_ns"] / 1e9 < cutoff]
                CHECKS[f"{arm}/{name}/{label}:native_text_equals_final"] = call["caller_return"]["result_text"] == final["data"]["text"]
                for key in ("submitted_pcm", "written_wav_pcm"):
                    p = stt_path.parent / call[key]["file"]
                    CHECKS[str(p.relative_to(ROOT)) + ":hash"] = hashlib.sha256(read(p)).hexdigest() == call[key]["sha256"]
                intervals = [{**ref(aec_path, pointer=f"/recognition_objects/{i}/intervals/{j}"), **v} for i, obj in enumerate(aec["recognition_objects"]) if obj["room"] == report["room"] for j, v in enumerate(obj["intervals"]) if v["last_monotonic"] >= onset and v["first_monotonic"] < cutoff]
                row = {"arm": arm, "case": name, "input_label": label, "focus": (arm, name, label) in FOCUS,
                       "comparison_role": "healthy first-baseline comparator" if arm == "A2_first" else "target" if (arm, name, label) in FOCUS else "retained context",
                       "room": report["room"], "input_ref": ref(report_path, pointer=f"/inputs/{turn_index}"),
                       "input_onset_monotonic": onset, "input_speech_end_monotonic": speech_end,
                       "old_handle": old, "next_utterance_queue": next_handle,
                       "timeline": timeline, "user_speaking_after_onset_ms": user["after_input_onset_ms"],
                       "user_listening_after_onset_ms": listening["after_input_onset_ms"],
                       "agent_listening_after_onset_ms": pause["after_input_onset_ms"] if pause else None,
                       "agent_speaking_again_after_onset_ms": resume["after_input_onset_ms"] if resume else None,
                       "agent_speaking_again_after_user_listening_ms": ms(resume["received_monotonic"], listening["received_monotonic"]) if resume else None,
                       "user_final_after_onset_ms": final["after_input_onset_ms"],
                       "old_handle_end_after_onset_ms": ms(old["ended_at"], onset),
                       "last_above_threshold_before_next_utterance": frame_ref(frame_path, loud[-1], onset),
                       "last_explicit_old_handle_tagged_frame": frame_ref(frame_path, tagged[-1], onset),
                       "old_audio_gap_observations_over_300ms": gaps,
                       "stt": {"source": call_ref, "call_id": call["call_id"], "boundaries": boundaries, "stages": stages, "requests": requests,
                               "submitted_pcm": call["submitted_pcm"], "written_wav_pcm": call["written_wav_pcm"],
                               "queue_ms": (call["worker_entry"]["monotonic_ns"]-call["submitted"]["monotonic_ns"])/1e6},
                       "aec_substitution_intervals_overlapping_followup": intervals}
                pairs.append(row)
    source_refs = []
    for relative, ranges, meaning in [
        ("agent/main.py", [[117,130],[158,173]], "Speech-handle end labels and forwarded user/agent state events; no explicit false-interruption/resume event sink."),
        ("agent/coordinator/coordinator.py", [[84,107],[116,124]], "Tentative user-speaking gates future work; finalized intent cancels active speech tasks."),
        ("scripts/local_livekit_audio_experiment.py", [[95,153]], "Queued handle/text pairing and PCM tags depend on current speaking state and sole active result."),
        ("agent/pipeline/local_stt.py", [[129,138],[141,163]], "Recognition returns final text after whole-segment inference; transcribe_since can return without native inference.")]:
        path = ROOT / "results/iteration17/source-snapshot" / relative
        read(path)
        source_refs.append({**ref(path), "line_ranges": ranges, "meaning": meaning})
    read(Path(__file__).resolve())
    result = {"created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "scope": "Offline reconstruction of all18 existing interruption pairs, with five requested slow examples and all six first-baseline comparisons identified. No new run, model reads, tests or historical edits.",
              "method": "Join room and input order to native recognition; match client events exactly to server type/data/ts; retain physical source line and JSON pointer references. PCM threshold is the original -40dB. Gaps >=300ms are descriptive threshold-activity gaps, not a new quality score.",
              "checks": CHECKS, "all_reconstruction_checks_pass": all(CHECKS.values()),
              "pair_counts": {"all":len(pairs), "focus":sum(p["focus"] for p in pairs), "first_baseline":sum(p["arm"]=="A2_first" for p in pairs)},
              "pairs": pairs, "worker_log_search": logs, "diagnostic_health": arm_health,
              "production_source_refs": source_refs, "evidence_sha256": HASHES,
              "findings": [
                  "All five selected slow examples have a client agent-state listening transition followed by speaking again, with a multi-second below-threshold PCM interval and subsequent received activity while native recognition remains pending.",
                  "Speaking-again receipts occur approximately two seconds after user-listening. This temporal pattern is consistent with a timeout-driven resume hypothesis; neither a timeout callback nor an explicit false-interruption/resume event was recorded, so those internal actions are not directly established.",
                  "The three B1 finished failures finish their old result before new final text arrives. Both interrupted-but-late examples resume activity before their later interruption-request receipt.",
                  "The 7.875s B1 and 4.917s return-baseline tails include untagged final frames. Recorder tags cease with agent-state listening before trailing PCM drains; explicit old-ID last frames and the broader pre-next-utterance interval are both retained.",
                  "The first baseline has six interruption-request receipts and no speaking-again transition between its initial post-input listening state and old-handle end.",
                  "Queue waits are short relative to the delayed native generate boundaries. This localizes observed elapsed time without assigning paging, cache, GPU or scheduling causation."
              ],
              "unanswered_causal_questions": [
                  "Did the SDK's false-interruption timer actually fire, and which internal branch resumed each original handle? Explicit callback/speech pause-resume observations were not captured.",
                  "What made native generate slow for these unmatched endpointed PCM inputs? Fixed-order cache arms do not establish causation.",
                  "How much of the last untagged tail is transport buffering versus state-event delivery order? Frames carry no utterance ID and RTP/data channels are not ordered together.",
                  "How would real microphone echo, noise, partial speech or an empty final result change safe pause/resume behavior? These synthetic navigation inputs do not answer that.",
                  "Would changing finalization or pause policy preserve recovery from false/noise interruptions without premature cancellation? No candidate policy is tested here."
              ],
              "limits": [
                  "Input onset/end are sample-threshold positions on the scheduled publish clock, not acoustic arrival at the worker.",
                  "Same-host monotonic clocks align these process receipts; there is no independent hardware clock synchronization or physical playout observation.",
                  "Agent/user state events are server proxies. Listening plus below-threshold PCM is observed; an internal SpeechHandle pause/resume transition is inferred, not instrumented.",
                  "All above-threshold track frames before the next utterance queue are retained, including untagged tail. This is a bounded activity attribution heuristic, not sample-level utterance authentication.",
                  "Threshold gaps retain intervening low-level frames and callback spacing; they cannot prove physical silence or intelligibility.",
                  "Host generate intervals include possible lazy execution and synchronization and must not be summed with enclosing native intervals.",
                  "No explicit pause/resume log match means the retained INFO traces do not expose the mechanism; it does not mean no pause/resume occurred."
              ]}
    with OUT.open("x") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    print(json.dumps({"output":str(OUT.relative_to(ROOT)), "checks":len(CHECKS), "all_checks_pass":all(CHECKS.values()), "pairs":len(pairs), "focus":sum(p["focus"] for p in pairs)}, indent=2))


if __name__ == "__main__":
    main()
