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
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from .crawl import _item_path
from .http import get as http_get
from .robots import get_policy

# A sitemap can list a hundred thousand URLs; we only need enough to fill the
# caller's cap, and parsing the rest is time nobody asked for.
MAX_SITEMAP_BYTES = 12_000_000
MAX_SITEMAPS = 25
# Breadcrumbs run home > section > category; the nearest few are the ones
# that actually list pages like the example.
MAX_LISTINGS = 8
# A breadcrumb trail is home > section > category, not a site map. More links
# than this and the container is something else wearing the name.
MAX_CRUMBS = 8
# Three links of one shape prove a template; one proves nothing.
MIN_TEMPLATE_GROUP = 3
# How deep to follow a listing's pagination while hunting for items.
MAX_LISTING_PAGES = 5


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


def canonical(url: str, shape: UrlShape | None = None,
              html: str | None = None) -> str:
    """One name per page, so the same car is not fetched twice.

    A share widget appends ?share=x and a tracker appends ?utm_source=y; both
    are the page the caller already has. On one dealer site two of fifty-two
    links were duplicates of that kind. A page that declares a canonical URL
    is believed; otherwise every query parameter the example did not itself
    use is dropped, since those are the ones that cannot be identifying.
    """
    if html:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("link", rel=lambda v: v and "canonical" in (
            v if isinstance(v, list) else [v]))
        href = (tag.get("href") or "").strip() if tag else ""
        if href:
            return urljoin(url, href.split("#")[0])

    parts = urlsplit(url)
    keep = shape.query_keys if shape is not None else frozenset()
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k in keep])
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/",
                       query, ""))


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


def _links_on(url: str, shape: UrlShape, fetcher=None, session=None,
              html: str | None = None) -> list[str]:
    if html is None:
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


def _breadcrumb_links(html: str | None, base_url: str) -> list[str]:
    """Where the example says it came from.

    An item page nearly always points back at its category, in a breadcrumb.
    That category is a listing, and a listing can be walked with pagination --
    which is the difference between the four books a product page happens to
    link to and the whole shelf.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for container in soup.select(
            '[class*=breadcrumb], [id*=breadcrumb], nav[aria-label*=readcrumb]'):
        anchors = container.find_all("a", href=True)
        # A trail is short: home, section, category. One dealer's footer sat
        # in a wrapper whose class contained "breadcrumb", and its nineteen
        # links -- privacy policy, disclaimer, copyright -- filled every slot
        # the real stock page needed.
        if not anchors or len(anchors) > MAX_CRUMBS:
            continue
        for a in anchors:
            url = urljoin(base_url, a["href"].split("#")[0])
            if url not in seen and urlsplit(url).netloc == urlsplit(base_url).netloc:
                seen.add(url)
                out.append(url)
    # Nearest first: the last crumb is the category, the first is the home page.
    return list(reversed(out))


def siblings_on(links: list[str], example_url: str, shape: UrlShape,
                min_group: int = MIN_TEMPLATE_GROUP) -> list[str]:
    """Which of a listing's links are items like the example.

    The strict shape -- every segment equal but the last -- is right for
    /products/<slug> and wrong for most real catalogues. A dealer writes
    /occasions/voertuig/<id>/<slug>, where two segments differ between any
    two cars; another writes /<brand>/<model>/<trim-id>/<year>/1/1/
    details.aspx, where five do. Against those the strict rule matched
    nothing, which is how by-example found zero pages on five sites out of
    seven while their stock lists sat there full of cars.

    With a listing in hand the listing decides. Among its links of the same
    depth as the example, each is scored by how many path segments it shares
    with the example in the same position: the cars share the route and
    differ on the identifier, while /about/team/jan/bio shares only the
    leading empty segment. The best score wins, and it has to be reached by
    a group -- three links of a shape prove a template, one proves nothing.
    """
    host = urlsplit(example_url).netloc
    example = _item_path(example_url).split("/")

    scored: list[tuple[int, str]] = []
    for url in links:
        if urlsplit(url).netloc != host:
            continue
        segments = _item_path(url).split("/")
        if len(segments) != len(example):
            continue
        agreement = sum(1 for a, b in zip(segments, example) if a == b)
        scored.append((agreement, url))

    # Best agreement first, taking whole score bands until there are enough
    # links to call it a template, then stopping. Stopping is the point: one
    # sibling may share an extra segment by luck -- the same brand, the same
    # year -- and cutting at the top score alone would keep only that one,
    # while going all the way down would sweep in /about/team/jan/bio.
    group: list[str] = []
    for score in sorted({agreement for agreement, _ in scored}, reverse=True):
        group.extend(url for agreement, url in scored if agreement == score)
        if len(group) >= min_group:
            return group

    return [url for url in links if shape.matches(url)]


def _all_links_on(url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        candidate = urljoin(url, a["href"].split("#")[0])
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _fetch_listing(url: str, example_url: str, shape: UrlShape,
                   fetcher=None, session=None, js_fetcher=None,
                   min_group: int = MIN_TEMPLATE_GROUP) -> tuple[str | None, list[str]]:
    """The listing page and the item links on it, rendering if it has to.

    A stock list drawn by a script has no links in its static HTML at all,
    so the static read is not evidence of absence -- retry in a browser
    before deciding this page is not the listing.
    """
    html = fetcher.fetch(url) if fetcher is not None else _text_of(url, session)
    items = (siblings_on(_all_links_on(url, html), example_url, shape, min_group)
             if html else [])
    if not items and js_fetcher is not None:
        rendered = js_fetcher.fetch(url)
        if rendered:
            html = rendered
            items = siblings_on(_all_links_on(url, html), example_url, shape,
                                min_group)
    return html, items


def _from_listings(shape: UrlShape, example_url: str, example_html: str | None,
                   limit: int, fetcher=None, session=None, js_fetcher=None,
                   listing_url: str | None = None) -> Iterator[str]:
    """Item URLs from a listing, following its pagination.

    Which listing, in order of how much we trust it: the one the caller
    named, then the breadcrumb on the example page, then each directory
    above the example. A level with no item links is not the listing, so the
    walk climbs rather than giving up -- most dealers keep their stock
    several levels above the car.

    The listing's own links decide what an item looks like here; see
    :func:`siblings_on`. Frontier is used only for its pagination, because
    its link-picking heuristics are tuned for shops and miss a car whose id
    sits in a directory of its own.
    """
    from .crawl import Frontier          # late: crawl imports from here

    # Named first, then the directories above the example -- those are
    # derived from the URL and cannot be anything else -- and only then the
    # breadcrumb, which is a reading of the page and can be wrong.
    candidates: list[str] = []
    if listing_url:
        candidates.append(listing_url)
    for url in (_ancestor_listings(example_url)
                + _breadcrumb_links(example_html, example_url)):
        if url not in candidates:
            candidates.append(url)

    frontier = Frontier(fetcher=fetcher, session=session, js_fetcher=js_fetcher)
    found = 0
    for listing in candidates[:MAX_LISTINGS]:
        page, pages_walked = listing, 0
        while page and pages_walked < MAX_LISTING_PAGES:
            pages_walked += 1
            # A listing the caller named is vouched for, so two cars on it
            # are enough; a guessed one has to prove itself with a group.
            html, items = _fetch_listing(
                page, example_url, shape, fetcher, session, js_fetcher,
                min_group=2 if listing == listing_url else MIN_TEMPLATE_GROUP)
            if not items:
                break
            for url in items:
                found += 1
                yield url
                if found >= limit:
                    return
            page = frontier._next_page(BeautifulSoup(html, "html.parser"), page)
        if found:
            return          # that was the listing; no need to climb further


def _ancestor_listings(example: str) -> list[str]:
    """Every directory above the example, nearest first.

    Trying only the immediate parent found nothing on most real dealer
    sites: a car at /occasions/voertuig/56896140/volkswagen-taigo has its
    stock list at /occasions, three levels up, while
    /occasions/voertuig/56896140 is a directory that does not exist.
    Climbing costs one request per level and stops at the level that
    answers.

    Written without a trailing slash: servers that care redirect between the
    two forms and the fetcher follows redirects, but a framework that 404s
    on one of them is more often the slashed one.
    """
    parts = urlsplit(example)
    origin = f"{parts.scheme}://{parts.netloc}"
    segments = _item_path(example).strip("/").split("/")
    out = [f"{origin}/" + "/".join(segments[:depth])
           for depth in range(len(segments) - 1, 0, -1)]
    out.append(f"{origin}/")
    return out


def find_similar(example_url: str, *, limit: int = 100, fetcher=None,
                 session=None, example_html: str | None = None,
                 js_fetcher=None, listing_url: str | None = None) -> list[str]:
    """URLs of pages shaped like ``example_url``, cheapest source first.

    ``example_html`` is the example page if the caller already holds it.
    Without it this fetches the page a second time, and the caller pays for
    both. ``listing_url`` is the stock or results page, when the caller knows
    it -- worth saying, because guessing it is the part that fails. With
    ``js_fetcher`` a listing that renders client-side is retried in a browser.
    """
    shape = url_template(example_url)
    parts = urlsplit(example_url)
    origin = f"{parts.scheme}://{parts.netloc}"

    out: list[str] = []
    seen: set[str] = set()

    def take(urls: Iterable[str]) -> bool:
        for url in urls:
            # No shape test here: every source has already decided what
            # counts as a sibling, and a listing's own verdict is looser than
            # the strict shape on purpose. Re-testing it here quietly threw
            # away everything the listing had found.
            #
            # Two links to one page -- ?share=x, ?utm_source=y -- are one
            # page, and fetching it twice bills the caller twice.
            key = canonical(url, shape)
            if key in seen:
                continue
            seen.add(key)
            out.append(url)
            if len(out) >= limit:
                return True
        return False

    seen.add(canonical(example_url, shape))

    # A named listing is better than anything we could infer, so it goes
    # first -- ahead even of the sitemap, which on a big site is mostly pages
    # the caller did not ask about.
    if listing_url and take(_from_listings(shape, example_url, example_html,
                                           limit, fetcher, session, js_fetcher,
                                           listing_url)):
        return out

    if take(_from_sitemaps(shape, origin, limit, session)):
        return out

    # One read of the example serves both remaining sources: its own links,
    # and the breadcrumb that says which category it belongs to.
    if example_html is None:
        example_html = (fetcher.fetch(example_url) if fetcher is not None
                        else _text_of(example_url, session))
    if take(_links_on(example_url, shape, fetcher, session, example_html)):
        return out
    # Last and most thorough: walk the category the example came from. This
    # one follows pagination, so it reaches past whatever the item page
    # happened to link to -- on a demo shop, the same four books every time.
    take(_from_listings(shape, example_url, example_html, limit - len(out),
                        fetcher, session, js_fetcher))
    return out
