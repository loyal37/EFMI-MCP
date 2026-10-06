"""Background jobs for the model workflow: extraction, LoD mapping, export, verification.

A job runs in its own thread so a tool call can return an ID at once; its
stage, messages, result or error are read back with the job tool. Cancelling
is cooperative: the job checks between steps, and a running Blender process is
stopped. Only one job runs at a time, because they share the probe connection
and the project folders.
"""
from __future__ import annotations

import contextlib
import io
import itertools
import threading
import time
import traceback


class JobCancelled(RuntimeError):
    pass


class Job:
    def __init__(self, job_id: str, kind: str, summary: str, uses_probe: bool = True):
        self.id = job_id
        self.kind = kind
        self.summary = summary
        self.uses_probe = uses_probe     # other tools must leave the probe alone meanwhile
        self.status = "running"          # running | done | failed | cancelled
        self.stage = "starting"
        self.messages: list[str] = []
        self.result = None
        self.error = ""
        self.started = time.time()
        self.finished = None
        self.thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._process = None
        self._lock = threading.Lock()

    # -- used by the job's own code ---------------------------------------
    def step(self, stage: str) -> None:
        """Start a step; raises JobCancelled when the job was asked to stop."""
        self.check()
        self.stage = stage
        self.messages.append(f"{time.strftime('%H:%M:%S')} {stage}")
        del self.messages[:-60]

    def check(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled("cancelled")

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def attach_process(self, process) -> None:
        with self._lock:
            self._process = process

    # -- used by the tool --------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            process = self._process
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    def describe(self, messages: int = 12) -> dict:
        info = {"job_id": self.id, "kind": self.kind, "summary": self.summary, "status": self.status,
                "stage": self.stage, "elapsed_s": round((self.finished or time.time()) - self.started, 1),
                "recent": self.messages[-messages:]}
        if self.status == "done":
            info["result"] = self.result
        elif self.status in ("failed", "cancelled"):
            info["error"] = self.error
            if self.result is not None:
                info["partial"] = self.result
        return info


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def active(self) -> Job | None:
        with self._lock:
            return next((job for job in self._jobs.values() if job.status == "running"), None)

    def start(self, kind: str, summary: str, function, uses_probe: bool = True) -> Job:
        """Run ``function(job)`` in a thread. Refuses while another job runs."""
        with self._lock:
            running = next((job for job in self._jobs.values() if job.status == "running"), None)
            if running is not None:
                raise RuntimeError(f"Job {running.id} ({running.kind}: {running.stage}) is still running. "
                                   "Wait for it or cancel it first.")
            job = Job(f"{kind}-{next(self._counter)}", kind, summary, uses_probe)
            self._jobs[job.id] = job

        def run():
            try:
                # The server's stdout carries the protocol; a library that
                # prints from this thread must not write into it.
                with contextlib.redirect_stdout(io.StringIO()):
                    job.result = function(job)
                job.status = "done"
                job.stage = "finished"
            except JobCancelled:
                job.status, job.error = "cancelled", "cancelled on request"
            except Exception as exc:
                # The cancel request may surface as whatever error killing a
                # subprocess or closing a pipe produced.
                if job.cancelled():
                    job.status, job.error = "cancelled", f"cancelled on request ({exc})"
                else:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.messages.append(traceback.format_exc(limit=6))
            finally:
                job.finished = time.time()

        job.thread = threading.Thread(target=run, name=job.id, daemon=True)
        job.thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self) -> Job | None:
        with self._lock:
            return list(self._jobs.values())[-1] if self._jobs else None

    def all(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())
