"""Find every page shaped like the one you pasted.

Crawling normally starts from a listing: "here is the category, walk it". But
people do not think in listings. They think "here is the page I care about,
get me all of these" -- one car, one job post, one product -- and on plenty of
sites the listing either does not exist as a URL, or its cards carry no link
to follow (autoscout24 links only to the dealer).

So this starts from one example and works outwards:

1. **The sitemap**, if the site publishes one. It is the site's own list of
   its pages, it is free, and it is exact. robots.txt usually names it; when
   it does not, ``/sitemap.xml`` is the convention. A sitemap index is a
   sitemap of sitemaps, so one level of nesting is followed.
2. **The example page's own links** -- "related items", "more from this
   seller", the next result -- filtered to the same shape.
3. **The parent directory** of the example URL, which on most sites is the
   listing the example came from.

What "the same shape" means is the interesting part; see :func:`url_template`.

Nothing here calls a model. Finding the pages is arithmetic on URLs; only
reading them costs anything, and that is one recipe for the whole set.
"""

from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .crawl import _item_path
from .http import get as http_get
from .robots import get_policy

# A sitemap can list a hundred thousand URLs; we only need enough to fill the
# caller's cap, and parsing the rest is time nobody asked for.
MAX_SITEMAP_BYTES = 12_000_000
MAX_SITEMAPS = 25


class UrlShape:
    """What it means for two URLs to be "the same kind of page".

    Two rules, because sites split cleanly into two camps:

    * **Path-shaped** -- ``/products/blue-lamp`` and ``/products/red-chair``.
      Same host, same depth, every segment equal except the last.
    * **Query-shaped** -- ``/detail?id=1234``. Same host, same path, same set
      of query keys; the values are what vary.

    Deliberately strict about depth. ``/products/lamps`` (a sub-category) has
    the same prefix as ``/products/blue-lamp`` but is a different kind of
    page, and a looser rule drags category pages into a product export.
    """

    def __init__(self, example: str):
        parts = urlsplit(example)
        self.host = parts.netloc
        self.path = _item_path(example)
        segments = self.path.split("/")
        self.prefix = segments[:-1]
        self.depth = len(segments)
        self.query = parts.query
        self.query_keys = frozenset(k for k, _ in parse_qsl(parts.query))
        # A trailing segment that is purely numeric (an id) says nothing about
        # the others, but a slug-shaped one at least tells us not to accept a
        # bare number where a name belongs.
        self.tail_is_numeric = bool(re.fullmatch(r"\d+", segments[-1] or ""))

    def matches(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.netloc != self.host or parts.scheme not in ("http", "https"):
            return False
        path = _item_path(url)
        if (path, parts.query) == (self.path, self.query):
            return False                     # the example itself

        if self.query_keys:
            # Query-shaped: the path is fixed and the parameters vary, so
            # comparing paths alone would reject every sibling as a repeat.
            return (path == self.path
                    and frozenset(k for k, _ in parse_qsl(parts.query)) == self.query_keys)

        segments = path.split("/")
        if len(segments) != self.depth or segments[:-1] != self.prefix:
            return False
        tail = segments[-1]
        if not tail:
            return False
        return bool(re.fullmatch(r"\d+", tail)) == self.tail_is_numeric


def url_template(example: str) -> UrlShape:
    """The shape an example URL stands for."""
    return UrlShape(example)


# ── sources ──────────────────────────────────────────────────────────────────
def _text_of(url: str, session=None) -> str | None:
    try:
        response = http_get(url, session=session)
    except requests.RequestException:
        return None
    if getattr(response, "status_code", 0) != 200:
        return None
    body = response.content or b""
    if len(body) > MAX_SITEMAP_BYTES:
        return None
    if body[:2] == b"\x1f\x8b":              # served as .xml.gz
        try:
            body = gzip.decompress(body)
        except OSError:
            return None
    return body.decode("utf-8", "replace")


def _sitemap_urls(origin: str, session=None) -> list[str]:
    """Where this site says its sitemaps are, plus the conventional guess."""
    found: list[str] = []
    policy = get_policy()
    if policy is not None:
        parser = policy._parser_for(origin)
        listed = getattr(parser, "site_maps", lambda: None)() if parser else None
        found.extend(listed or [])
    guess = f"{origin}/sitemap.xml"
    if guess not in found:
        found.append(guess)
    return found[:MAX_SITEMAPS]


def _parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Return ``(page urls, nested sitemap urls)``. Namespaces vary; ignore them."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return [], []
    pages, nested = [], []
    index = root.tag.rsplit("}", 1)[-1] == "sitemapindex"
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "loc":
            continue
        value = (element.text or "").strip()
        if not value:
            continue
        # In a sitemap index every <loc> is another sitemap; in a plain
        # sitemap they are pages, whatever they happen to end with.
        (nested if index else pages).append(value)
    return pages, nested


def _from_sitemaps(shape: UrlShape, origin: str, limit: int,
                   session=None) -> Iterator[str]:
    seen_maps: set[str] = set()
    queue = list(_sitemap_urls(origin, session))
    found = 0
    while queue and found < limit:
        sitemap = queue.pop(0)
        if sitemap in seen_maps or len(seen_maps) >= MAX_SITEMAPS:
            continue
        seen_maps.add(sitemap)
        xml = _text_of(sitemap, session)
        if xml is None:
            continue
        pages, nested = _parse_sitemap(xml)
        for page in pages:
            if shape.matches(page):
                found += 1
                yield page
                if found >= limit:
                    return
        # Follow a sitemap index, but only into maps that could hold our pages.
        queue.extend(n for n in nested if n not in seen_maps)


def _links_on(url: str, shape: UrlShape, fetcher=None, session=None) -> list[str]:
    html = fetcher.fetch(url) if fetcher is not None else _text_of(url, session)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        candidate = urljoin(url, a["href"].split("#")[0])
        if candidate not in seen and shape.matches(candidate):
            seen.add(candidate)
            out.append(candidate)
    return out


def _parent_listing(example: str) -> str:
    parts = urlsplit(example)
    parent = _item_path(example).rsplit("/", 1)[0] or "/"
    return f"{parts.scheme}://{parts.netloc}{parent}"


def find_similar(example_url: str, *, limit: int = 100, fetcher=None,
                 session=None) -> list[str]:
    """URLs of pages shaped like ``example_url``, cheapest source first."""
    shape = url_template(example_url)
    parts = urlsplit(example_url)
    origin = f"{parts.scheme}://{parts.netloc}"

    out: list[str] = []
    seen: set[str] = set()

    def take(urls: Iterable[str]) -> bool:
        for url in urls:
            if url in seen or not shape.matches(url):
                continue
            seen.add(url)
            out.append(url)
            if len(out) >= limit:
                return True
        return False

    if take(_from_sitemaps(shape, origin, limit, session)):
        return out
    if take(_links_on(example_url, shape, fetcher, session)):
        return out
    take(_links_on(_parent_listing(example_url), shape, fetcher, session))
    return out
