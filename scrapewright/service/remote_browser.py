"""Use the browser that lives on the other machine.

Drop-in for :class:`~scrapewright.fetch.BrowserFetcher`: same `fetch(url)`,
same "None means no content" contract, so nothing in the pipeline knows the
difference.

It falls back to rendering in this process when the renderer cannot be
reached. That is a deliberate choice and worth defending: the point of the
split is to keep secrets away from the browser, and falling back puts the
browser next to them again. But an unreachable renderer would otherwise
take `js=true` away from every customer at once, and the failure most
likely to happen is my own -- a bad deploy of a service that is four days
old, not a Chromium exploit. The fallback is loud in the log, so it is a
thing that can be noticed and fixed rather than a thing that silently
becomes the normal state.
"""

from __future__ import annotations

import logging
import os

import requests

from ..fetch import PageGone

log = logging.getLogger("scrapewright.service")

RENDER_URL_ENV = "RENDER_URL"
RENDER_TOKEN_ENV = "RENDER_TOKEN"
# Generous: a render of a heavy page with scrolling takes tens of seconds,
# and the caller is already holding a browser slot while it waits.
DEFAULT_TIMEOUT = 120


class RemoteBrowserFetcher:
    """Renders by asking the render service, with a local last resort."""

    def __init__(self, base_url: str, token: str = "", *, max_scrolls: int = 0,
                 timeout: int = DEFAULT_TIMEOUT, local_fallback=None,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.max_scrolls = max_scrolls
        self.timeout = timeout
        self._local_fallback = local_fallback
        self._session = session or requests.Session()
        self.used_fallback = False

    @property
    def alive(self) -> bool:
        """Nothing to go stale here: the browser is somebody else's problem."""
        return True

    def fetch(self, url: str) -> str | None:
        try:
            response = self._session.post(
                f"{self.base_url}/render",
                json={"url": url, "scroll": self.max_scrolls},
                headers={"X-Render-Token": self.token},
                timeout=self.timeout)
        except requests.RequestException as e:
            return self._fall_back(url, f"could not reach it: {e}")

        if response.status_code == 503:
            # The renderer is up and busy. Waiting is the caller's business;
            # rendering here instead would defeat its own slot limit.
            log.warning("renderer is busy; no fallback for a full queue")
            return None
        if response.status_code != 200:
            return self._fall_back(url, f"answered {response.status_code}")

        try:
            body = response.json()
        except ValueError as e:
            return self._fall_back(url, f"answered nonsense: {e}")
        if body.get("gone"):
            raise PageGone(url, body["gone"])
        return body.get("html")

    def _fall_back(self, url: str, why: str) -> str | None:
        log.error("render service unusable (%s) -- rendering in this process, "
                  "which puts a browser next to the secrets again", why)
        if self._local_fallback is None:
            return None
        self.used_fallback = True
        return self._local_fallback.fetch(url)

    def close(self) -> None:
        if self._local_fallback is not None:
            self._local_fallback.close()


def remote_renderer_configured() -> bool:
    return bool(os.environ.get(RENDER_URL_ENV))
