"""Ordered per-room JSONL I/O outside the audio event loop.

Submission snapshots an event but does not acknowledge persistence. A successful
close drains all accepted lines and closes the file; flush is not fsync. A native
file operation can outlive the close deadline and still delay process exit.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path

from agent.coordinator.events import Event

log = logging.getLogger("prism.trace")


class TraceWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.accepted = self.written = self.rejected = 0
        self._loop = asyncio.get_running_loop()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace")
        self._file = None
        self._failure: Exception | None = None
        self._reported_failure = False
        self._started = False
        self._closing: asyncio.Future | None = None

    def _submit(self, function, *args) -> asyncio.Future:
        future = self._loop.run_in_executor(self._pool, function, *args)
        future.add_done_callback(self._observe)
        return future

    def _observe(self, future: asyncio.Future) -> None:
        if future.cancelled():
            return
        error = future.exception()
        if error is not None and not self._reported_failure:
            self._reported_failure = True
            log.error("trace I/O failed: %s", self.path, exc_info=(type(error), error, error.__traceback__))

    def _open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a")
        except Exception as error:
            self._failure = error
            raise

    async def start(self) -> None:
        if self._started or self._closing is not None:
            raise RuntimeError("trace writer already started or closing")
        self._started = True
        await asyncio.shield(self._submit(self._open))

    def __call__(self, event: Event) -> None:
        if not self._started or self._closing is not None or self._failure is not None:
            self.rejected += 1
            if self.rejected == 1:
                log.error("trace event rejected: %s (not started, closing or failed)", self.path)
            return
        # Serialize before another sink/caller can mutate nested event data.
        try:
            payload = json.dumps(event.to_dict(), default=str) + "\n"
            self._submit(self._write, payload)
        except Exception:
            self.rejected += 1
            log.exception("trace event submission failed: %s", self.path)
            return
        self.accepted += 1

    def _write(self, payload: str) -> None:
        # Do not append after an uncertain partial write/flush or retry it.
        if self._failure is not None:
            return
        try:
            self._file.write(payload)
            self._file.flush()
            self.written += 1
        except Exception as error:
            self._failure = error
            raise

    def _close(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
        except Exception as error:
            self._failure = error
            raise

    async def aclose(self, timeout: float = 5.0) -> bool:
        if self._closing is None:
            self._closing = self._submit(self._close)
            # Keep queued operations, including close. Never join a blocked file
            # operation on the event loop, or cancel a future and hide its result.
            self._pool.shutdown(wait=False)
        _, pending = await asyncio.wait({self._closing}, timeout=timeout)
        complete = not pending and self._failure is None and self.accepted == self.written and not self.rejected
        log.log(logging.INFO if complete else logging.ERROR,
                "trace cleanup: %s complete=%s accepted=%s flushed=%s rejected=%s pending=%s",
                self.path, complete, self.accepted, self.written, self.rejected, bool(pending))
        return complete
