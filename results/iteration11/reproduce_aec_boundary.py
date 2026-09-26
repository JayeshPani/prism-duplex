"""Installed-SDK routing reproduction with dummy channels, no model or RTC service."""
import asyncio
from array import array
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import inspect
import json
from pathlib import Path
import sys
from types import MethodType, SimpleNamespace

from livekit import rtc
from livekit.agents.voice.agent_activity import AgentActivity
from livekit.agents.voice.agent_session import AgentSession, _DEFAULT_AEC_WARMUP_DURATION
from livekit.agents.voice.audio_recognition import AudioRecognition


async def main():
    output = Path(__file__).with_name("aec-boundary-reproduction.json")
    if output.exists():
        raise FileExistsError(output)
    vad_frames, stt_frames = [], []
    session = SimpleNamespace(
        agent_state="speaking", _aec_warmup_remaining=_DEFAULT_AEC_WARMUP_DURATION,
        _aec_warmup_timer=None, _closing=False, amd=None,
        options=SimpleNamespace(interruption={"discard_audio_if_uninterruptible": True}),
    )
    recognition = SimpleNamespace(
        _stt_pipeline=SimpleNamespace(
            input_started_at=None, audio_ch=SimpleNamespace(send_nowait=stt_frames.append)),
        _vad_ch=SimpleNamespace(send_nowait=vad_frames.append), _session=session,
        _interruption_ch=None, _turn_detector_stream=None,
    )
    recognition._push_audio = MethodType(AudioRecognition._push_audio, recognition)
    activity = SimpleNamespace(
        _started=True, _session=session, _current_speech=None,
        _rt_session=None, _audio_recognition=recognition,
    )
    pcm = array("h", [1000, -1000] * 160).tobytes()
    frame = rtc.AudioFrame(data=pcm, sample_rate=16000, num_channels=1, samples_per_channel=320)
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
              "scope": "Actual installed SDK methods with dummy channels; no model, VAD inference, network, native recognition or full session.",
              "packages": {name: version(name) for name in ["livekit", "livekit-agents"]},
              "default_warmup_seconds": _DEFAULT_AEC_WARMUP_DURATION, "rows": []}
    sources = [Path(__file__), *[Path(inspect.getsourcefile(cls)) for cls in
                                [AgentActivity, AgentSession, AudioRecognition]]]
    report["source_sha256"] = {str(p): sha256(p.read_bytes()).hexdigest() for p in sources}

    def observe(label):
        AgentActivity.push_audio(activity, frame)
        row = {"label": label, "monotonic": asyncio.get_running_loop().time(),
               "warmup_remaining": session._aec_warmup_remaining,
               "warmup_timer_active": session._aec_warmup_timer is not None,
               "vad_received_original_object": vad_frames[-1] is frame,
               "vad_pcm_matches_input": bytes(vad_frames[-1].data) == pcm,
               "stt_received_original_object": stt_frames[-1] is frame,
               "stt_pcm_matches_input": bytes(stt_frames[-1].data) == pcm,
               "stt_pcm_all_zero": not any(bytes(stt_frames[-1].data)),
               "frame_dimensions_preserved": all(
                   (f.sample_rate, f.num_channels, f.samples_per_channel) == (16000, 1, 320)
                   for f in [vad_frames[-1], stt_frames[-1]])}
        report["rows"].append(row)
        print(json.dumps(row), flush=True)

    try:
        observe("before_warmup_timer")
        expired = asyncio.Event()

        def expire():
            AgentSession._on_aec_warmup_expired(session)
            expired.set()

        session._aec_warmup_timer = asyncio.get_running_loop().call_later(
            _DEFAULT_AEC_WARMUP_DURATION, expire)
        observe("during_warmup_timer")
        await asyncio.wait_for(expired.wait(), timeout=_DEFAULT_AEC_WARMUP_DURATION + 2)
        observe("after_actual_timer_expired")
        before, during, after = report["rows"]
        assert all(r["vad_pcm_matches_input"] and r["frame_dimensions_preserved"] for r in report["rows"])
        assert before["stt_pcm_matches_input"] and after["stt_pcm_matches_input"]
        assert during["stt_pcm_all_zero"] and not during["stt_pcm_matches_input"]
        assert after["warmup_remaining"] == 0 and not after["warmup_timer_active"]
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error=repr(exc))
        raise
    finally:
        if session._aec_warmup_timer is not None:
            session._aec_warmup_timer.cancel()
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["source_hashes_unchanged"] = all(sha256(p.read_bytes()).hexdigest() == report["source_sha256"][str(p)] for p in sources)
        report["model_module_prefixes_present"] = [name for name in ["torch", "mlx", "parakeet_mlx", "kokoro_onnx"] if name in sys.modules]
        with output.open("x") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")


if __name__ == "__main__":
    asyncio.run(main())
