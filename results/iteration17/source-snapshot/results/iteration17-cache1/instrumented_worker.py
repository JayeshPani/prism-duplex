"""Retain STT stage evidence around the existing AEC diagnostic launcher."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    output = args.manifest.parent / "stt-diagnostics"
    if output.exists():
        parser.error("STT diagnostics exist; use a new experiment directory")

    from livekit.agents import get_job_context
    from parakeet_mlx import parakeet
    from agent.pipeline import local_stt
    from stt_diagnostics import STTDiagnostics

    source = ROOT / "results/iteration11/instrumented_worker.py"
    spec = importlib.util.spec_from_file_location("iteration11_aec_worker", source)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    observer = STTDiagnostics(get_job_context)
    exit_receipt = {"started_at_utc": datetime.now(timezone.utc).isoformat()}
    launcher_error = None
    try:
        observer.install(local_stt, parakeet)
        launcher.main()
        exit_receipt["status"] = "launcher_returned"
    except BaseException as error:
        launcher_error = error
        exit_receipt.update(status="launcher_exited", exception=repr(error))
        raise
    finally:
        observer.restore()
        try:
            exit_receipt["diagnostics_saved"] = observer.flush(output)
            if not exit_receipt["diagnostics_saved"]:
                exit_receipt["diagnostic_error"] = observer.flush_error
                print(f"STT diagnostic flush failed: {observer.flush_error}", file=sys.stderr)
        except BaseException as error:
            exit_receipt.update(diagnostics_saved=False, diagnostic_error=repr(error))
            traceback.print_exc()
            if launcher_error is None:
                raise
        finally:
            exit_receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            with (args.manifest.parent / "stt-diagnostics-exit.json").open("x") as stream:
                json.dump(exit_receipt, stream, indent=2)
                stream.write("\n")


if __name__ == "__main__":
    main()
