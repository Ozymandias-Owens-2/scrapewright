"""Crawl frontier: turn one listing/category URL into a stream of product URLs.

Page mode alone answers "extract THIS page"; the frontier answers "walk the
whole store". It is deliberately deterministic and free — no LLM is involved in
*finding* product pages, only (at most once per site) in extracting them.

Discovery heuristics, in order:

1. **URL-pattern match** — links whose path looks like a product route
   (``/products/<slug>``, ``/item/<slug>``, ``/p/<slug>``, ...). Covers most
   stores regardless of platform.
2. **Template grouping fallback** — when no path pattern matches, links whose
   anchor wraps an ``<img>`` (i.e. product cards) are grouped by their parent
   path; the largest group of at least :data:`MIN_GROUP` unique URLs is taken
   as the product template. This catches bespoke routes like ``/shop/<slug>``.

Pagination follows ``<link rel=next>`` / ``<a rel=next>`` / a "next"-labeled
anchor, up to ``max_listing_pages``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .fetch import StaticFetcher, looks_js_shelled

PRODUCT_PATH = re.compile(r"/(?:products?|item|itm|prod|p)/[^/?#]+/?$", re.IGNORECASE)
# A directory index is the page *for* its directory, not a page of its own.
# Static generators -- Jekyll, Hugo, and every catalogue exported to flat
# files -- serve items as `/thing/index.html`, which defeated both heuristics
# below: the pattern looked for a slug and found a filename, and the grouping
# read each item's own directory as the template, so every group held one
# member and the crawl came back empty.
INDEX_FILE = re.compile(r"/index\.[a-z0-9]{1,5}$", re.IGNORECASE)
NEXT_LABELS = {"next", "next page", "›", "»", "→", ">", "older"}
MIN_GROUP = 3


def _item_path(href: str) -> str:
    """The path that identifies an item, with any directory index removed."""
    return INDEX_FILE.sub("", urlparse(href).path.rstrip("/")) or "/"


class Frontier:
    """Turns one listing URL into a stream of product URLs.

    ``js_fetcher`` is optional: when a listing renders its grid client-side,
    the static fetch yields no product links and the frontier retries that page
    in a browser rather than giving up.
    """

    def __init__(self, fetcher=None, session: requests.Session | None = None,
                 js_fetcher=None, max_listing_pages: int = 5):
        self.fetcher = fetcher or StaticFetcher(session)
        self.js_fetcher = js_fetcher
        self.max_listing_pages = max_listing_pages

    # ── public ───────────────────────────────────────────────────────────────
    def discover(self, listing_url: str) -> Iterator[str]:
        """Yield unique product-page URLs found under a listing, following
        pagination up to ``max_listing_pages``."""
        seen_products: set[str] = set()
        seen_pages: set[str] = set()
        url: str | None = listing_url

        for _ in range(self.max_listing_pages):
            if url is None or url in seen_pages:
                break
            seen_pages.add(url)

            html = self._fetch_listing(url)
            if html is None:
                break
            soup = BeautifulSoup(html, "html.parser")

            for product_url in self._product_links(soup, url):
                if product_url not in seen_products:
                    seen_products.add(product_url)
                    yield product_url

            url = self._next_page(soup, url)

    def _fetch_listing(self, url: str) -> str | None:
        """Static first; escalate to the browser when the page is a JS shell or
        exposes no product links at all."""
        html = self.fetcher.fetch(url)
        if self.js_fetcher is None:
            return html
        if html is None or looks_js_shelled(html):
            return self.js_fetcher.fetch(url) or html
        soup = BeautifulSoup(html, "html.parser")
        if not self._product_links(soup, url):
            return self.js_fetcher.fetch(url) or html
        return html

    # ── link extraction ──────────────────────────────────────────────────────
    def _same_host_links(self, soup: BeautifulSoup, base_url: str) -> list[tuple[str, object]]:
        host = urlparse(base_url).netloc
        base_path = urlparse(base_url).path or "/"
        out, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"].split("#")[0])
            p = urlparse(href)
            if p.netloc != host or p.path == base_path:
                continue
            if href in seen:
                continue
            seen.add(href)
            out.append((href, a))
        return out

    def _product_links(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        links = self._same_host_links(soup, base_url)

        # 1) Path-pattern match — the cheap, high-precision route.
        matched = [href for href, _ in links if PRODUCT_PATH.search(_item_path(href))]
        if matched:
            return matched

        # 2) Fallback: product cards (anchor wraps an image), grouped by their
        #    parent path — the dominant template is the product route.
        groups: dict[str, list[str]] = defaultdict(list)
        for href, a in links:
            if a.find("img") is None:
                continue
            parent = _item_path(href).rsplit("/", 1)[0] or "/"
            groups[parent].append(href)

        if not groups:
            return []
        parent, members = max(groups.items(), key=lambda kv: len(kv[1]))
        return members if len(members) >= MIN_GROUP else []

    # ── pagination ───────────────────────────────────────────────────────────
    def _next_page(self, soup: BeautifulSoup, base_url: str) -> str | None:
        link = soup.find("link", rel=lambda v: v and "next" in v)
        if link and link.get("href"):
            return urljoin(base_url, link["href"])
        a = soup.find("a", rel=lambda v: v and "next" in v)
        if a and a.get("href"):
            return urljoin(base_url, a["href"])
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True).lower() in NEXT_LABELS:
                return urljoin(base_url, a["href"])
        return None
