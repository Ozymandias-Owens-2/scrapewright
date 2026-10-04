"""Three ways a watched page lied, found on the second live run.

A sold item does not 404 -- it redirects to the home page, and we read a
price off whatever card was first there. A WooCommerce sale puts the old
price inside <del> and the selector matched it first. And a run of HTTP
400s from the model provider looked, from the outside, like six broken
sites.
"""
from decimal import Decimal

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.llm import ProviderError
from scrapewright.extract.selectors import SelectorExtractor, pick_price_element
from scrapewright.fetch import PageGone, StaticFetcher, redirected_away
from scrapewright.schema import Schema

URL = "https://shop.test/p/1"


# ── 1. the item that is no longer there ──────────────────────────────────────
GONE = [
    # camperland.nl: a sold caravan, 301 to a sister dealer's home page
    ("https://camperland.nl/actueel-aanbod/caravelair-alba-450-996305/",
     "https://www.mobiledrome.nl/"),
    # holtkamp-west.nl: 302 to another host's root
    ("https://holtkamp-west.nl/camper/dreamer-d62-select-1058567/",
     "https://holtkampdewiers.nl/"),
    # back to the listing the item sat on
    ("https://camperland.nl/actueel-aanbod/caravelair-alba-450-996305/",
     "https://camperland.nl/actueel-aanbod/"),
    # and to the same host's root
    ("https://shop.test/product/thing", "https://shop.test/"),
]

STILL_FINE = [
    ("http://shop.test/product/thing", "https://shop.test/product/thing"),
    ("https://shop.test/product/thing", "https://www.shop.test/product/thing"),
    ("https://shop.test/product/thing", "https://shop.test/product/thing/"),
    # a renamed slug at the same depth is a move, not a disappearance
    ("https://shop.test/product/thing", "https://shop.test/product/thing-2024"),
    ("https://shop.test/product/thing", "https://shop.test/items/thing"),
    # a shorter path that is not an ancestor
    ("https://shop.test/shop/thing", "https://shop.test/thing"),
    # the root itself cannot vanish into itself
    ("https://shop.test/", "https://shop.test/"),
]


@pytest.mark.parametrize("requested,final", GONE)
def test_a_redirect_to_somewhere_shallower_means_gone(requested, final):
    assert redirected_away(requested, final)


@pytest.mark.parametrize("requested,final", STILL_FINE)
def test_ordinary_redirects_still_work(requested, final):
    assert not redirected_away(requested, final)


class _Redirecting:
    """A session that answers 200 from a different URL than the one asked."""

    def __init__(self, final):
        self.final = final

    def get(self, url, **kw):
        r = requests.Response()
        r.status_code = 200
        r.url = self.final
        r._content = b"<html><body>welcome to our dealership</body></html>"
        r.headers["content-type"] = "text/html; charset=utf-8"
        return r


def test_the_static_fetcher_refuses_to_read_the_replacement(monkeypatch):
    monkeypatch.setattr("scrapewright.safeurl.check_url", lambda url: None)
    monkeypatch.setattr("scrapewright.robots.check", lambda url: None)
    fetcher = StaticFetcher(session=_Redirecting("https://www.mobiledrome.nl/"))
    with pytest.raises(PageGone) as caught:
        fetcher.fetch("https://camperland.nl/actueel-aanbod/alba-450-996305/")
    assert caught.value.final == "https://www.mobiledrome.nl/"


def test_a_crawl_steps_over_one_vanished_item():
    """One sold caravan out of a thousand must not end the run."""
    from scrapewright.pipeline import Scrapewright

    sw = Scrapewright(fetcher=StaticFetcher())
    sw.extract = lambda *a, **k: (_ for _ in ()).throw(
        PageGone("https://shop.test/a", "https://shop.test/"))
    assert sw._extract_or_skip("https://shop.test/a", Schema.from_names(["x"]),
                               False) is None


# ── 2. the selector that matched the old price first ─────────────────────────
ARIADNA = """
<p class="price">
  <del aria-hidden="true"><span class="woocommerce-Price-amount">250,00&euro;</span></del>
  <ins><span class="woocommerce-Price-amount">199,00&euro;</span></ins>
</p>"""


def _extract(html, selector):
    schema = Schema.from_names(["price:number"])
    recipe = SelectorRecipe(fields={"price": selector}, schema_name=schema.name)
    return SelectorExtractor(recipe, schema).extract_values(html, URL)


def test_the_match_inside_del_is_not_the_price():
    """deportesariadna.com: "p.price .woocommerce-Price-amount" matches twice
    and the first one is struck through. Which selector the model writes is a
    lottery; which match is current should not be."""
    assert _extract(ARIADNA, "p.price .woocommerce-Price-amount")["price"] \
        == Decimal("199.00")


def test_the_selector_that_already_worked_still_works():
    assert _extract(ARIADNA, "p.price ins .woocommerce-Price-amount")["price"] \
        == Decimal("199.00")


def test_every_match_struck_still_answers():
    """Excluding candidates is for choosing between them, not for emptying a
    field whose markup merely resembles a discount."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup('<div class="normal"><span class="p">9,99</span></div>',
                         "html.parser")
    assert pick_price_element(soup.select(".p")).get_text() == "9,99"


def test_a_struck_card_price_on_a_listing():
    schema = Schema.from_names(["title", "price:number"])
    recipe = SelectorRecipe(fields={"title": "h2", "price": ".amount"},
                            item="li", schema_name=schema.name)
    rows = SelectorExtractor(recipe, schema).extract_rows(
        '<ul><li><h2>Boots</h2><p class="price">'
        '<del><span class="amount">250,00</span></del>'
        '<ins><span class="amount">199,00</span></ins></p></li></ul>', URL)
    assert rows[0].data["price"] == Decimal("199.00")


# ── 3. the provider's bad evening ────────────────────────────────────────────
def test_a_provider_failure_is_not_a_verdict_on_the_page():
    """Six synthesis calls came back HTTP 400 and the same six pages went
    through minutes later."""
    from scrapewright.extract.llm import LlmExtractor

    class _Angry:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("BadRequestError: 400 upstream")

    with pytest.raises(ProviderError) as caught:
        LlmExtractor(client=_Angry()).synthesize("<html></html>", URL)
    assert "400" in caught.value.detail


def test_a_failed_call_is_not_a_synthesis_anybody_pays_for():
    from scrapewright.service.metering import Meter, _CountingLlm

    class _Angry:
        def synthesize(self, html, url, schema=None, **kw):
            raise ProviderError("400")

    meter = Meter()
    with pytest.raises(ProviderError):
        _CountingLlm(_Angry(), meter).synthesize("<html>", URL)
    assert meter.syntheses == 0


def _billable(store, key):
    """What this key would actually be charged for."""
    used = store.usage_for_day(key.id)
    return used.records, used.renders, used.syntheses


def _service(monkeypatch, tmp_path, raising):
    """A service whose pipeline raises, so the HTTP answer can be read."""
    from scrapewright.service import metering
    from scrapewright.service.app import create_app
    from scrapewright.service.jobs import JobRegistry
    from scrapewright.service.store import Store

    class _Raising:
        def extract(self, url, schema, **kw):
            raise raising
        def close(self):
            pass

    monkeypatch.setattr("scrapewright.service.app.metered_scrapewright",
                        lambda **kw: (_Raising(), metering.Meter()))
    store = Store(tmp_path / "svc.db")
    raw, key = store.create_key(label="t", plan="metered")
    client = TestClient(create_app(store=store, jobs=JobRegistry(max_workers=1)))
    client.headers.update({"X-API-Key": raw})
    return client, store, key


def test_a_vanished_page_answers_410_and_costs_nothing(monkeypatch, tmp_path):
    gone = PageGone("https://camperland.nl/actueel-aanbod/alba-450-996305/",
                    "https://www.mobiledrome.nl/")
    client, store, key = _service(monkeypatch, tmp_path, gone)
    r = client.post("/v1/extract", json={"url": gone.requested})
    assert r.status_code == 410
    assert "home page" in r.json()["detail"]
    assert _billable(store, key) == (0, 0, 0)


def test_a_redirect_to_the_listing_says_so(monkeypatch, tmp_path):
    gone = PageGone("https://shop.test/stock/car-1", "https://shop.test/stock/")
    client, _, _ = _service(monkeypatch, tmp_path, gone)
    r = client.post("/v1/extract", json={"url": gone.requested})
    assert r.status_code == 410 and "listing" in r.json()["detail"]


def test_a_provider_outage_answers_503_with_retry_after(monkeypatch, tmp_path):
    client, store, key = _service(monkeypatch, tmp_path,
                                  ProviderError("BadRequestError: 400 upstream"))
    r = client.post("/v1/extract", json={"url": "https://shop.test/p/1"})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    assert _billable(store, key) == (0, 0, 0)


def test_the_provider_text_reaches_the_log(monkeypatch, tmp_path, caplog):
    client, _, _ = _service(monkeypatch, tmp_path,
                            ProviderError("BadRequestError: 400 upstream"))
    with caplog.at_level("ERROR"):
        client.post("/v1/extract", json={"url": "https://shop.test/p/1"})
    assert "400 upstream" in caplog.text
