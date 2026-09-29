"""Tiny HTTP helper so every extractor fetches the same polite way.

Every request in the library goes through :func:`get`, which is also where
robots.txt is honoured -- one choke point rather than a rule each extractor has
to remember.
"""

from __future__ import annotations

from urllib.parse import urljoin

import requests
from requests.exceptions import TooManyRedirects

from ._version import __version__

# Identify honestly. The old string claimed to be Mozilla, which is both untrue
# and self-defeating: robots.txt rules are addressed to a named agent, and a
# crawler hiding behind a browser string cannot be given permission by name.
USER_AGENT = (f"scrapewright/{__version__} "
              "(+https://github.com/Ozymandias-Owens-2/scrapewright)")
DEFAULT_TIMEOUT = 15
MAX_REDIRECTS = 10


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _decoded(response):
    """Believe the bytes over the header when the header never said anything.

    For ``text/*`` with no charset, requests falls back to ISO-8859-1 as HTTP
    1.1 once required. Most of the web is UTF-8 and says so only in a <meta>
    tag, so that default turns every pound sign and accent into mojibake --
    books.toscrape.com serves UTF-8 under a bare ``text/html`` and a price came
    back as a run of Latin-1 gibberish.

    The test is a strict UTF-8 decode rather than statistical detection:
    UTF-8 is self-validating, so a body that decodes cleanly essentially is
    UTF-8, while a detector handed a short page will cheerfully answer Big5.
    Anything that fails the decode keeps whatever requests worked out.
    """
    headers = getattr(response, "headers", None) or {}
    if "charset=" in headers.get("content-type", "").lower():
        return response                      # the server was explicit; obey it
    try:
        response.content.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return response
    except Exception:                        # a test double without .content
        return response
    try:
        response.encoding = "utf-8"
    except Exception:
        pass
    return response


def get(url: str, session: requests.Session | None = None, **kw) -> requests.Response:
    """Fetch a URL, unless the site's robots.txt says not to.

    Raises :class:`~scrapewright.robots.RobotsDisallowed`, which is a
    ``RequestException``, so existing error handling degrades to "no content"
    rather than breaking.
    """
    kw.setdefault("timeout", DEFAULT_TIMEOUT)
    from .safeurl import check_url

    check_url(url)
    if not url.endswith("/robots.txt"):
        from . import robots        # late: robots.py imports this module
        robots.check(url)
    sess = session or make_session()

    # Redirects are followed by hand so each hop can be checked. Letting
    # requests follow them means a public URL can bounce us to 127.0.0.1 and
    # the only address anybody validated was the one the caller typed.
    kw["allow_redirects"] = False
    for _ in range(MAX_REDIRECTS):
        response = sess.get(url, **kw)
        # Read defensively: injected sessions and test doubles hand back
        # response-like objects that do not implement every attribute, and a
        # fetcher should not demand that they do.
        redirecting = getattr(response, "is_redirect", False)
        headers = getattr(response, "headers", None) or {}
        location = headers.get("location") if redirecting else None
        if not location:
            return _decoded(response)
        url = urljoin(url, location)
        check_url(url)
        response.close()
    raise TooManyRedirects(f"more than {MAX_REDIRECTS} redirects from {url}")
