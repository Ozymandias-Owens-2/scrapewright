from pathlib import Path

from scrapewright.cache import RecipeCache
from scrapewright.crawl import Frontier
from scrapewright.pipeline import Scrapewright
from tests.test_pipeline import _FakeLLM, GENERIC_RECIPE

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeResponse:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        import json
        return json.loads(self.text)   # raises ValueError on HTML → detect handles it


class _MappedSession:
    """URL → HTML map; anything unmapped is a 404 (so platform probes fail
    cleanly and detect() lands on 'generic')."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.requests = []

    def get(self, url, **kw):
        self.requests.append(url)
        if url in self.mapping:
            return _FakeResponse(self.mapping[url])
        # probe URLs carry query strings — match on the bare URL too
        bare = url.split("?")[0]
        if bare in self.mapping:
            return _FakeResponse(self.mapping[bare])
        return _FakeResponse("not found", status=404)


def _fx(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


BASE = "https://boutique.example.com"

LISTING_MAP = {
    f"{BASE}/collection": _fx("listing_page1.html"),
    f"{BASE}/collection?page=2": _fx("listing_page2.html"),
}


def test_frontier_discovers_across_pagination():
    frontier = Frontier(session=_MappedSession(LISTING_MAP))
    urls = list(frontier.discover(f"{BASE}/collection"))
    assert urls == [
        f"{BASE}/products/alpha-coat",     # deduped: appears twice on page 1
        f"{BASE}/products/beta-boots",
        f"{BASE}/products/gamma-shirt",
        f"{BASE}/products/delta-hat",      # from page 2 via rel=next
    ]


def test_frontier_respects_max_listing_pages():
    frontier = Frontier(session=_MappedSession(LISTING_MAP), max_listing_pages=1)
    urls = list(frontier.discover(f"{BASE}/collection"))
    assert f"{BASE}/products/delta-hat" not in urls
    assert len(urls) == 3


def test_frontier_fallback_template_grouping():
    """No /products/ pattern → the largest image-card group wins (/shop/...),
    and the lone /journal/ card is not swept in."""
    session = _MappedSession({f"{BASE}/shop": _fx("listing_fallback.html")})
    urls = list(Frontier(session=session).discover(f"{BASE}/shop"))
    assert sorted(urls) == [
        f"{BASE}/shop/item-one",
        f"{BASE}/shop/item-three",
        f"{BASE}/shop/item-two",
    ]


def test_crawl_end_to_end_jsonld(tmp_path):
    """Custom site whose product pages carry JSON-LD: full crawl, zero LLM."""
    product_html = _fx("jsonld_product.html")
    mapping = dict(LISTING_MAP)
    for slug in ("alpha-coat", "beta-boots", "gamma-shirt", "delta-hat"):
        mapping[f"{BASE}/products/{slug}"] = product_html

    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm,
                      session=_MappedSession(mapping))

    products = list(sw.crawl(f"{BASE}/collection"))
    assert len(products) == 4
    assert all(p.is_usable() for p in products)
    assert llm.calls == 0                    # JSON-LD covered everything for free


def test_crawl_synthesizes_once_then_replays(tmp_path):
    """Custom site with NO JSON-LD: the first page pays the one synthesis,
    every other page replays the cached recipe."""
    product_html = _fx("generic_product.html")
    mapping = dict(LISTING_MAP)
    for slug in ("alpha-coat", "beta-boots", "gamma-shirt", "delta-hat"):
        mapping[f"{BASE}/products/{slug}"] = product_html

    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm,
                      session=_MappedSession(mapping))

    products = list(sw.crawl(f"{BASE}/collection"))
    assert len(products) == 4
    assert llm.calls == 1                    # compile once, replay three times


def test_crawl_max_items(tmp_path):
    product_html = _fx("jsonld_product.html")
    mapping = dict(LISTING_MAP)
    for slug in ("alpha-coat", "beta-boots", "gamma-shirt", "delta-hat"):
        mapping[f"{BASE}/products/{slug}"] = product_html

    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"),
                      llm=_FakeLLM(GENERIC_RECIPE),
                      session=_MappedSession(mapping))
    products = list(sw.crawl(f"{BASE}/collection", max_items=2))
    assert len(products) == 2
