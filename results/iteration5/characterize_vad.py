"""Characterize every frozen probe; never select live cases from VAD output."""
import asyncio
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent


async def main():
    from livekit import rtc
    from livekit.plugins import silero

    output = BASE / "vad-characterization.json"
    if output.exists():
        raise FileExistsError(output)
    sdk = Path(silero.__file__).parent
    paths = [Path(__file__), ROOT / "agent/main.py", sdk / "vad.py", sdk / "onnx_model.py",
             sdk / "resources/silero_vad.onnx", BASE / "protocol.json"]
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "source_sha256": {str(p): sha256(p.read_bytes()).hexdigest() for p in paths},
              "packages": {p: version(p) for p in ("livekit", "livekit-agents", "livekit-plugins-silero", "onnxruntime")},
              "configuration": {"min_speech_duration": .05, "min_silence_duration": .45},
              "method": "Fresh stream per probe, 0.5s leading/3s trailing zeros; feed complete PCM without wall-clock pacing; retain all VAD events.",
              "limitations": ["Direct VAD inference does not include RTC encoding, room history, STT or SDK interruption policy.",
                              "Every frozen case still runs live regardless of this output; no threshold or waveform tuning."],
              "cases": []}
    output.write_text(json.dumps(report, indent=2) + "\n")
    vad = silero.VAD.load(**report["configuration"])
    for name in ("silence", "noise", "tone", "fragment"):
        path = BASE / "audio-assets" / f"{name}.wav"
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            pcm = wav.readframes(wav.getnframes())
        stream = vad.stream()
        events = []

        async def collect():
            async for event in stream:
                events.append({"type": event.type.value, "samples_index": event.samples_index,
                               "timestamp": event.timestamp, "speech_duration": event.speech_duration,
                               "silence_duration": event.silence_duration, "probability": event.probability,
                               "speaking": event.speaking, "raw_accumulated_speech": event.raw_accumulated_speech,
                               "raw_accumulated_silence": event.raw_accumulated_silence})

        task = asyncio.create_task(collect())
        try:
            padded = bytes(rate) + pcm + bytes(rate * 6)
            step = rate * 20 // 1000 * 2
            for start in range(0, len(padded), step):
                chunk = padded[start:start + step]
                stream.push_frame(rtc.AudioFrame(chunk, rate, 1, len(chunk) // 2))
            stream.end_input()
            await asyncio.wait_for(task, 30)
        finally:
            await stream.aclose()
        case = {"id": name, "input_sha256": sha256(path.read_bytes()).hexdigest(),
                "event_counts": dict(Counter(e["type"] for e in events)),
                "maximum_probability": max((e["probability"] for e in events), default=None),
                "events": events}
        report["cases"].append(case)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in case.items() if k != "events"}), flush=True)
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
