"""Background jobs, because a crawl outlives an HTTP request.

Detecting a platform takes a second; walking a store takes minutes. Holding a
connection open for the latter is how you collect timeouts, so crawls are
submitted, given an id, and polled.

Kept intentionally to a thread pool and an in-memory registry: the work is
I/O-bound, and a job queue that needs its own broker would be a heavier
dependency than the thing it schedules. Results expire so a long-lived process
does not grow without bound.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

RESULT_TTL_SECONDS = 60 * 60  # an hour is plenty to collect a crawl


@dataclass
class Job:
    id: str
    key_id: str
    kind: str
    status: str = "queued"          # queued | running | done | error
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: Any = None
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    def as_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        payload = {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "usage": self.usage,
        }
        if self.error:
            payload["error"] = self.error
        if include_result and self.status == "done":
            payload["result"] = self.result
        return payload


class JobRegistry:
    def __init__(self, max_workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="scrapewright-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, key_id: str, kind: str,
               work: Callable[[], tuple[Any, dict[str, int]]]) -> Job:
        """``work`` returns ``(result, usage)`` and runs off the request thread."""
        job = Job(id=uuid.uuid4().hex[:16], key_id=key_id, kind=kind)
        with self._lock:
            self._prune_locked()
            self._jobs[job.id] = job
        self._pool.submit(self._run, job, work)
        return job

    def _run(self, job: Job, work: Callable[[], tuple[Any, dict[str, int]]]) -> None:
        job.status = "running"
        try:
            result, usage = work()
            job.result, job.usage, job.status = result, usage, "done"
        except Exception as e:  # a failed job is data, not a crashed server
            job.error, job.status = f"{type(e).__name__}: {e}", "error"
        finally:
            job.finished_at = time.time()

    def get(self, job_id: str, key_id: str | None = None) -> Job | None:
        job = self._jobs.get(job_id)
        # A job id must not be a capability: only its owner may read it.
        if job is None or (key_id is not None and job.key_id != key_id):
            return None
        return job

    def list_for(self, key_id: str) -> list[Job]:
        with self._lock:
            return sorted((j for j in self._jobs.values() if j.key_id == key_id),
                          key=lambda j: j.created_at, reverse=True)

    def _prune_locked(self) -> None:
        cutoff = time.time() - RESULT_TTL_SECONDS
        stale = [jid for jid, j in self._jobs.items()
                 if j.finished_at and j.finished_at < cutoff]
        for jid in stale:
            del self._jobs[jid]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
