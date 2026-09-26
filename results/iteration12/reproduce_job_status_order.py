"""Exercise installed SDK shutdown/status methods with fake channels and no services."""
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import inspect
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

from livekit.agents.ipc.job_executor import JobStatus
from livekit.agents.ipc.job_proc_lazy_main import _JobProc, _ShutdownInfo
from livekit.agents.ipc.job_thread_executor import ThreadJobExecutor
from livekit.agents.worker import AgentServer
from livekit.protocol import agent


async def trial(callback_raises):
    events, errors = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    executor = object.__new__(ThreadJobExecutor)
    executor._job_status = JobStatus.RUNNING
    executor._running_job = SimpleNamespace(job=SimpleNamespace(id="AJ_dummy"))

    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                errors.append({"message": record.getMessage(),
                               "exception": repr(record.exc_info[1]) if record.exc_info else None})

    capture = Capture()
    logger = logging.getLogger("livekit.agents")
    logger.addHandler(capture)

    async def append(label):
        events.append({"event": label, "executor_status": executor.status.value})

    async def cleanup(reason):
        await append("application_cleanup_started")
        entered.set()
        await release.wait()
        if callback_raises:
            await append("application_cleanup_raised")
            raise RuntimeError("deliberate dummy cleanup failure")
        await append("application_cleanup_finished")

    async def send(message):
        await append("ipc_" + type(message).__name__)

    async def queue(message):
        events.append({"event": "worker_status_queued",
                       "wire_status": agent.JobStatus.Name(message.update_job.status),
                       "executor_status": executor.status.value})

    async def ping():
        await asyncio.Future()

    async def monitor():
        return

    async def no_work():
        return

    context = SimpleNamespace(
        _primary_agent_session=SimpleNamespace(aclose=lambda: append("session_aclose")),
        _on_session_end=lambda: append("context_session_end"),
        _shutdown_callbacks=[cleanup],
    )
    proc = SimpleNamespace(_job_ctx=context, _session_end_fnc=None,
                           _client=SimpleNamespace(send=send),
                           _room=SimpleNamespace(disconnect=lambda: append("room_disconnect")))
    worker = SimpleNamespace(_queue_msg=queue)
    entry = asyncio.create_task(no_work())
    await entry
    task = asyncio.create_task(_JobProc._shutdown_job(
        proc, entry, _ShutdownInfo(user_initiated=True, reason="dummy requested shutdown")))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await AgentServer._update_job_status(worker, executor)
        held_status = executor.status.value
        release.set()
        await asyncio.wait_for(task, timeout=2)
        # The real thread's join is replaced by an already-resolved future here.
        executor._initialize_fut = asyncio.get_running_loop().create_future()
        executor._initialize_fut.set_result(None)
        executor._join_fut = asyncio.get_running_loop().create_future()
        executor._join_fut.set_result(None)
        executor._ping_task, executor._monitor_task = ping, monitor
        executor._inference_tasks = set()
        executor._pch = SimpleNamespace(aclose=lambda: append("parent_ipc_close"))
        await asyncio.wait_for(ThreadJobExecutor._main_task(executor), timeout=2)
        await AgentServer._update_job_status(worker, executor)
        order = [row["event"] for row in events]
        assert order.index("ipc_Exiting") < order.index("room_disconnect") < order.index("application_cleanup_started")
        assert held_status == "running"
        assert executor.status == JobStatus.SUCCESS
        assert [row["wire_status"] for row in events if "wire_status" in row] == ["JS_RUNNING", "JS_SUCCESS"]
        assert bool(errors) == callback_raises
        return {"callback_raises": callback_raises, "events": events,
                "captured_sdk_errors": errors, "assertions": "passed"}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        logger.removeHandler(capture)


async def main():
    output = Path(__file__).with_name("job-status-order-reproduction.json")
    if output.exists():
        raise FileExistsError(output)
    sources = [Path(__file__), *[Path(inspect.getsourcefile(cls)) for cls in
                                [_JobProc, ThreadJobExecutor, AgentServer]]]
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "packages": {name: version(name) for name in ["livekit", "livekit-agents"]},
              "source_sha256": {str(p): sha256(p.read_bytes()).hexdigest() for p in sources},
              "scope": "Actual installed SDK methods with dummy room, context, channels and resolved thread-join future. No live server, network, model, actual worker thread or Go server execution.",
              "trials": []}
    try:
        for callback_raises in [False, True]:
            result = await trial(callback_raises)
            report["trials"].append(result)
            print(json.dumps(result), flush=True)
        report["status"] = "passed"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["model_module_prefixes_present"] = [x for x in ["torch", "mlx", "parakeet_mlx", "kokoro_onnx"] if x in sys.modules]
        report["source_hashes_unchanged"] = all(sha256(p.read_bytes()).hexdigest() == report["source_sha256"][str(p)] for p in sources)
        with output.open("x") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")


if __name__ == "__main__":
    asyncio.run(main())
