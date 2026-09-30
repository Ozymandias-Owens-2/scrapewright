"""robots.txt: ask the site what it allows before taking it.

The rules follow RFC 9309, including the two that read backwards until you
check the text:

* **4xx, including 401 and 403, means allowed** (§2.3.1.3: the file is
  "unavailable", and "the crawler MAY access any resources on the server").
  Not a loophole. A bucket or CDN that never had a robots.txt commonly
  answers 403 rather than 404, and reading that as "this whole origin is
  forbidden" blocks images and assets nobody meant to protect. A site that
  wants to refuse a crawler says so *inside* robots.txt.
* **5xx or a network error means forbidden** (§2.3.1.4: the file is
  "unreachable", and "the crawler MUST assume complete disallow"). The rules
  might say no, and a server that cannot answer is not permission.

This module had those two exactly the wrong way round until an S3 bucket
answering 403 made it refuse to fetch images.

The unreachable verdict is held only briefly, not for the life of the
process: one stray 503 should not lock a long crawl out of a site that is
back a minute later. The RFC permits downgrading to "unavailable" after "a
reasonably long period"; this is the cautious version of the same idea at a
much shorter interval, since re-asking costs one request.

One fetch per origin otherwise, so a crawl of a thousand pages asks once.
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

from .http import DEFAULT_TIMEOUT, USER_AGENT

# How long an "unreachable" verdict stands before we ask again. Short on
# purpose: a 503 during a long crawl should cost a pause, not the rest of the
# site. Long enough that a site which is really down is not hammered.
UNREACHABLE_RETRY_SECONDS = 300


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
        # origin -> (rules or None, expiry or None for permanent)
        self._cache: dict[str, tuple[RobotFileParser | None, float | None]] = {}

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
        cached = self._cache.get(origin)
        if cached is not None:
            parser, expires = cached
            if expires is None or time.monotonic() < expires:
                return parser
        parser, temporary = self._load(origin)
        self._cache[origin] = (
            parser, time.monotonic() + UNREACHABLE_RETRY_SECONDS if temporary else None)
        return parser

    def _load(self, origin: str) -> tuple[RobotFileParser | None, bool]:
        """Returns ``(rules, is_temporary)``; ``None`` rules means no rules."""
        session = self.session or requests
        try:
            r = session.get(f"{origin}/robots.txt", timeout=self.timeout,
                            headers={"User-Agent": self.user_agent})
        except requests.RequestException:
            # Unreachable (RFC 9309 §2.3.1.4): complete disallow, but worth
            # asking again before writing the site off for good.
            return _deny_everything(), True

        if r.status_code >= 500:
            return _deny_everything(), True
        if r.status_code != 200:
            # Unavailable (§2.3.1.3): 404, and also the 401/403 that a bucket
            # with no robots.txt answers. No rules, so nothing forbids us.
            return None, False

        parser = RobotFileParser()
        parser.parse(r.text.splitlines())
        return parser, False


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
