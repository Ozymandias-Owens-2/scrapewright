"""Tiny HTTP helper so every extractor fetches the same polite way."""

from __future__ import annotations

import requests

USER_AGENT = "Mozilla/5.0 (compatible; scrapewright/0.1; +https://github.com/)"
DEFAULT_TIMEOUT = 15


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def get(url: str, session: requests.Session | None = None, **kw) -> requests.Response:
    kw.setdefault("timeout", DEFAULT_TIMEOUT)
    sess = session or make_session()
    return sess.get(url, **kw)
