"""robots.txt: ask the site what it allows before taking it.

The rules here follow RFC 9309, including the parts that are easy to get
backwards:

* No robots.txt (404) means everything is allowed. Absence is permission.
* A robots.txt that cannot be read because the server refuses us (401/403)
  means nothing is allowed. Refusal is not absence.
* A server error (5xx) is temporary and says nothing either way, so we allow
  and let the ordinary fetch fail if the site is genuinely down.

One fetch per origin, cached for the life of the policy, so a crawl of a
thousand pages asks once.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

from .http import DEFAULT_TIMEOUT, USER_AGENT

# 401/403 on robots.txt itself: the site is refusing to talk to us at all.
_REFUSED = frozenset({401, 403})


class RobotsDisallowed(requests.RequestException):
    """Raised instead of fetching a URL the site's robots.txt forbids.

    Subclasses ``RequestException`` on purpose: callers that already treat a
    failed fetch as "no content" keep working without a change, while callers
    that care can catch this specifically and say why they stopped.
    """


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


class RobotsPolicy:
    """Per-origin robots.txt rules, fetched once and remembered."""

    def __init__(self, user_agent: str = USER_AGENT,
                 session: requests.Session | None = None,
                 timeout: int = DEFAULT_TIMEOUT):
        self.user_agent = user_agent
        self.session = session
        self.timeout = timeout
        self._cache: dict[str, RobotFileParser | None] = {}

    # ── the questions worth asking ───────────────────────────────────────────
    def allows(self, url: str) -> bool:
        parser = self._parser_for(url)
        if parser is None:          # nothing to obey
            return True
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        """Seconds the site asks us to wait between requests, if it says."""
        parser = self._parser_for(url)
        if parser is None:
            return None
        delay = parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None

    # ── fetching and caching ─────────────────────────────────────────────────
    def _parser_for(self, url: str) -> RobotFileParser | None:
        origin = _origin(url)
        if origin not in self._cache:
            self._cache[origin] = self._load(origin)
        return self._cache[origin]

    def _load(self, origin: str) -> RobotFileParser | None:
        session = self.session or requests
        try:
            r = session.get(f"{origin}/robots.txt", timeout=self.timeout,
                            headers={"User-Agent": self.user_agent})
        except requests.RequestException:
            return None                      # unreachable: treat as absent

        if r.status_code in _REFUSED:
            return _deny_everything()
        if r.status_code != 200:
            return None                      # 404 absent, 5xx temporary

        parser = RobotFileParser()
        parser.parse(r.text.splitlines())
        return parser


def _deny_everything() -> RobotFileParser:
    parser = RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /"])
    return parser


# ── the default policy, and how to turn it off ───────────────────────────────
def _default_enabled() -> bool:
    return os.environ.get("SCRAPEWRIGHT_OBEY_ROBOTS", "1").lower() not in {
        "0", "false", "no"}


_policy: RobotsPolicy | None = RobotsPolicy() if _default_enabled() else None


def get_policy() -> RobotsPolicy | None:
    return _policy


def set_policy(policy: RobotsPolicy | None) -> None:
    """Replace the policy, or pass None to stop checking.

    Turning it off is a legitimate thing to do -- crawling your own site, or a
    client's with their written say-so -- which is why it is one call and not a
    patch. It is not the default, and a hosted service should never do it.
    """
    global _policy
    _policy = policy


def check(url: str) -> None:
    """Raise :class:`RobotsDisallowed` if the site forbids this URL."""
    policy = _policy
    if policy is not None and not policy.allows(url):
        raise RobotsDisallowed(f"robots.txt at {_origin(url)} disallows {url}")
