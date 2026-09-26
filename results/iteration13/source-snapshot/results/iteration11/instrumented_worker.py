"""Run the existing local worker with buffered STT-discard diagnostics only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import inspect
import json
from pathlib import Path
import sys
from threading import Lock
import time
from weakref import WeakKeyDictionary

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class FrameObserver:
    """Each recognition object belongs to its job loop; retain only aggregate data."""

    def __init__(self, get_context):
        self.get_context = get_context
        self.objects = WeakKeyDictionary()
        self.records = []
        self.errors = []
        self.error_count = 0
        self.observation_ns = 0
        self.max_observation_ns = 0
        self.lock = Lock()

    def record_error(self, error):
        self.error_count += 1
        if len(self.errors) < 20:
            self.errors.append(repr(error))

    def observe(self, recognition, frame, stt_frame):
        now, wall = time.monotonic(), time.time()
        row = self.objects.get(recognition)
        if row is None:
            row = {"recognition_index": len(self.records) + 1, "room": None,
                   "identity_error": None, "frames": 0, "samples_by_format": {},
                   "substituted_frames": 0, "substituted_samples_by_format": {},
                   "first_monotonic": now, "first_unix": wall, "intervals": [],
                   "active_interval": None}
            # Resolve once, including a failed lookup. Do not retry it per frame.
            self.objects[recognition] = row
            self.records.append(row)
            try:
                row["room"] = self.get_context().room.name
            except Exception as error:
                row["identity_error"] = repr(error)

        row["frames"] += 1
        row["last_monotonic"], row["last_unix"] = now, wall
        fmt = f"{frame.sample_rate}Hz/{frame.num_channels}ch"
        samples = frame.samples_per_channel
        row["samples_by_format"][fmt] = row["samples_by_format"].get(fmt, 0) + samples
        session = recognition._session
        aec = {"agent_state": session.agent_state,
               "remaining": session._aec_warmup_remaining,
               "timer_present": session._aec_warmup_timer is not None}
        aec["active"] = aec["agent_state"] == "speaking" and aec["remaining"] > 0 and aec["timer_present"]
        interval = row["active_interval"]
        if stt_frame is None:
            if interval is not None:
                interval.update(next_unsubstituted_monotonic=now, next_unsubstituted_unix=wall,
                                closed_by="next_unsubstituted_frame")
                row["active_interval"] = None
            return

        row["substituted_frames"] += 1
        counts = row["substituted_samples_by_format"]
        counts[fmt] = counts.get(fmt, 0) + samples
        if interval is None:
            interval = {"first_monotonic": now, "first_unix": wall,
                        "first_utc": datetime.fromtimestamp(wall, timezone.utc).isoformat(),
                        "frames": 0, "samples_by_format": {}, "aec_active_frames": 0,
                        "first_aec_state": aec, "first_substituted_frame_all_zero": not any(bytes(stt_frame.data)),
                        "first_original_frame_all_zero": not any(bytes(frame.data)),
                        "closed_by": None}
            row["intervals"].append(interval)
            row["active_interval"] = interval
        interval["frames"] += 1
        interval["samples_by_format"][fmt] = interval["samples_by_format"].get(fmt, 0) + samples
        interval["aec_active_frames"] += int(aec["active"])
        interval.update(last_monotonic=now, last_unix=wall, last_aec_state=aec)

    def wrap(self, original):
        def observed(recognition, frame, *, stt_frame=None):
            # Preserve the exact arguments, return value and exception behavior.
            result = original(recognition, frame, stt_frame=stt_frame)
            began = time.perf_counter_ns()
            with self.lock:
                try:
                    self.observe(recognition, frame, stt_frame)
                except Exception as error:
                    self.record_error(error)
                finally:
                    elapsed = time.perf_counter_ns() - began
                    self.observation_ns += elapsed
                    self.max_observation_ns = max(self.max_observation_ns, elapsed)
            return result
        return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    output = args.manifest.parent / "aec-discard-diagnostics.json"
    if output.exists():
        parser.error("diagnostics exist; use a new experiment directory")

    from livekit.agents import get_job_context
    from livekit.agents.voice.audio_recognition import AudioRecognition
    from livekit.agents.voice.agent_activity import AgentActivity
    from livekit.agents.voice.agent_session import AgentSession
    from scripts import local_livekit_worker

    sources = [Path(__file__), Path(local_livekit_worker.__file__),
               *[Path(inspect.getsourcefile(cls)) for cls in
                 [AudioRecognition, AgentActivity, AgentSession]]]
    hashes = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in sources}
    observer = FrameObserver(get_job_context)
    original = AudioRecognition._push_audio
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
              "packages": {name: version(name) for name in ["livekit", "livekit-agents"]},
              "source_sha256": hashes,
              "method": "Original AudioRecognition._push_audio runs unchanged before in-memory observation. No frame modification, model/turn/AEC setting override or live diagnostic file writes.",
              "limits": ["Instrumentation adds per-frame clock, counter, state-read and observer-lock overhead; timing is not comparable to historical uninstrumented runs as a causal experiment.",
                         "Sample counts are per channel, grouped by rate/channel format. Contiguous means consecutive substituted calls, not continuous wall-clock audio.",
                         "Only each interval's first substituted and original frames are checked for zero PCM. AEC-active state is captured on every substituted call.",
                         "Open intervals end at the last observed frame; worker shutdown does not imply an unsubstituted frame arrived.",
                         "Missing report after SIGKILL or abrupt process death means diagnostics are unavailable, not zero discarded frames.",
                         "No raw speech PCM is retained here; existing recorder artifacts remain the audio evidence."]}
    AudioRecognition._push_audio = observer.wrap(original)
    try:
        local_livekit_worker.main()
        report["status"] = "launcher_returned"
    except BaseException as error:
        report.update(status="launcher_exited", launcher_exception=repr(error))
        raise
    finally:
        AudioRecognition._push_audio = original
        room_totals = {}
        for row in observer.records:
            interval = row.pop("active_interval")
            if interval is not None:
                interval["closed_by"] = "report_finalization_without_next_unsubstituted_frame"
            room = row["room"] or f"unknown-recognition-{row['recognition_index']}"
            total = room_totals.setdefault(room, {"frames": 0, "substituted_frames": 0,
                                                 "samples_by_format": {}, "substituted_samples_by_format": {},
                                                 "recognition_indices": []})
            total["recognition_indices"].append(row["recognition_index"])
            for key in ["frames", "substituted_frames"]:
                total[key] += row[key]
            for key in ["samples_by_format", "substituted_samples_by_format"]:
                for fmt, count in row[key].items():
                    total[key][fmt] = total[key].get(fmt, 0) + count
        report.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),
                      recognition_objects=observer.records, room_totals=room_totals,
                      observation_error_count=observer.error_count, observation_errors=observer.errors,
                      observation_total_ns=observer.observation_ns,
                      observation_max_ns=observer.max_observation_ns,
                      source_hashes_unchanged=all(sha256(p.read_bytes()).hexdigest() == hashes[str(p.resolve())] for p in sources))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")


if __name__ == "__main__":
    main()
