"""How many browsers this process may run at once, which is not "any".

On 1 October a test client sent eight concurrent renders. Each request built
its own `BrowserFetcher`, so each started its own Chromium; the machine has
a gigabyte, Chromium wants a few hundred megabytes apiece, and the kernel
killed one. What it killed was the Playwright driver, so every later render
failed with "Connection closed while reading from the driver" -- and because
the health check also ran in the thread pool, nothing answered at all. One
client took the service down for everyone, for fifteen minutes.

Two things were missing and are here now.

**A ceiling.** One render at a time by default, from `BROWSER_SLOTS`. A
request that cannot get a slot within `BROWSER_WAIT_SECONDS` is told to come
back rather than queued forever behind a browser; a crawl, which is already
asynchronous, waits as long as it takes.

**One browser, many contexts.** Chromium is expensive to start and cheap to
isolate: a fresh context per request gives separate cookies and storage at
almost no cost. The browser is shared for the life of the process and
rebuilt if it dies, so a crash costs one request instead of the deployment.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager


def _slots() -> int:
    try:
        return max(1, int(os.environ.get("BROWSER_SLOTS", "1")))
    except ValueError:
        return 1


def _wait_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("BROWSER_WAIT_SECONDS", "20")))
    except ValueError:
        return 20.0


class NoBrowserSlot(RuntimeError):
    """Every browser slot is busy and waiting has not helped."""


class BrowserPool:
    """The process's browsers: how many may run, and the one that does."""

    def __init__(self, slots: int | None = None, wait: float | None = None):
        self.slots = slots or _slots()
        self.wait = _wait_seconds() if wait is None else wait
        self._semaphore = threading.BoundedSemaphore(self.slots)
        self._in_use = 0
        self._lock = threading.Lock()
        self._fetcher = None

    # ── the ceiling ──────────────────────────────────────────────────────────
    @contextmanager
    def slot(self, *, wait: float | None = None):
        """Hold a browser slot, or raise :class:`NoBrowserSlot`.

        ``wait=None`` uses the configured timeout; a crawl passes a large one
        because it is a background job and nobody is holding a connection
        open for it.
        """
        timeout = self.wait if wait is None else wait
        if not self._semaphore.acquire(timeout=timeout):
            raise NoBrowserSlot(
                f"all {self.slots} browser slots are busy. Rendering is the "
                f"expensive part and this instance runs {self.slots} at a "
                f"time; try again in a moment.")
        with self._lock:
            self._in_use += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_use -= 1
            self._semaphore.release()

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    # ── the browser ──────────────────────────────────────────────────────────
    def fetcher(self, **kwargs):
        """A fetcher sharing this process's Chromium.

        Rebuilt when the browser has died: the driver going away used to
        poison every later request until the machine was restarted.
        """
        from ..fetch import BrowserFetcher

        with self._lock:
            if self._fetcher is not None and not self._fetcher.alive:
                try:
                    self._fetcher.close()
                except Exception:
                    pass
                self._fetcher = None
            if self._fetcher is None:
                self._fetcher = BrowserFetcher(**kwargs)
            return self._fetcher

    def close(self) -> None:
        with self._lock:
            if self._fetcher is not None:
                try:
                    self._fetcher.close()
                finally:
                    self._fetcher = None


_pool: BrowserPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> BrowserPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = BrowserPool()
        return _pool


def set_pool(pool: BrowserPool | None) -> None:
    """For tests, and for a self-hosted process that wants its own limits."""
    global _pool
    with _pool_lock:
        _pool = pool
