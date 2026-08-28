"""Tiny HTTP helper so every extractor fetches the same polite way.

Every request in the library goes through :func:`get`, which is also where
robots.txt is honoured -- one choke point rather than a rule each extractor has
to remember.
"""

from __future__ import annotations

import requests

from ._version import __version__

# Identify honestly. The old string claimed to be Mozilla, which is both untrue
# and self-defeating: robots.txt rules are addressed to a named agent, and a
# crawler hiding behind a browser string cannot be given permission by name.
USER_AGENT = (f"scrapewright/{__version__} "
              "(+https://github.com/Ozymandias-Owens-2/scrapewright)")
DEFAULT_TIMEOUT = 15


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def get(url: str, session: requests.Session | None = None, **kw) -> requests.Response:
    """Fetch a URL, unless the site's robots.txt says not to.

    Raises :class:`~scrapewright.robots.RobotsDisallowed`, which is a
    ``RequestException``, so existing error handling degrades to "no content"
    rather than breaking.
    """
    kw.setdefault("timeout", DEFAULT_TIMEOUT)
    if not url.endswith("/robots.txt"):
        from . import robots        # late: robots.py imports this module
        robots.check(url)
    sess = session or make_session()
    return sess.get(url, **kw)
