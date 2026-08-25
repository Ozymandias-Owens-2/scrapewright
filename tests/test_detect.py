"""Platform detection: the routing decision every job starts with.

Fingerprint matching is pure, so it is tested against representative markup
rather than the network. The probe path (Shopify/WooCommerce catalog APIs) is
tested with a stub session.
"""

import pytest

from scrapewright.detect import (
    PLATFORMS,
    STRATEGY_CATALOG,
    STRATEGY_CRAWL,
    STRATEGY_CRAWL_JS,
    detect,
    match_fingerprints,
)

# One representative snippet per fingerprinted platform, in the shape it
# actually appears in the wild.
MARKUP = {
    "magento": '<script src="/static/version1699/frontend/Magento_Theme/js/x.js"></script>',
    "bigcommerce": '<script src="https://cdn11.bigcommerce.com/s-abc123/stencil/x.js"></script>',
    "salesforce-commerce": '<form action="/on/demandware.store/Sites-x-Site/en_US/Cart-Add">',
    "squarespace": '<img src="https://static1.squarespace.com/static/x/hero.jpg">',
    "wix": '<img src="https://static.wixstatic.com/media/abc.jpg"><script>wix-warmup-data</script>',
    "webflow": '<html data-wf-site="65a1b2c3"><script src="/js/webflow.js"></script>',
    "prestashop": '<link href="/modules/ps_searchbar/style.css"><body id="prestashop">',
    "shopware": '<script src="/bundles/storefront/js/app.js"></script>',
    "ecwid": '<script src="https://app.ecwid.com/script.js?12345"></script>',
    "opencart": '<a href="index.php?route=product/category&path=20">Shop</a>',
}


@pytest.mark.parametrize("platform,html", sorted(MARKUP.items()))
def test_each_platform_is_recognized(platform, html):
    assert platform in match_fingerprints(html)


def test_every_fingerprinted_platform_has_a_test_case():
    """A platform added to the registry without a fixture is untested — fail
    loudly rather than quietly shipping an unverified fingerprint."""
    fingerprinted = {p.name for p in PLATFORMS if p.fingerprints and not p.catalog_path}
    assert fingerprinted == set(MARKUP), fingerprinted.symmetric_difference(MARKUP)


def test_plain_html_matches_nothing():
    assert match_fingerprints("<html><body><h1>A blog</h1></body></html>") == []
    assert match_fingerprints("") == []


def test_headers_alone_can_identify_a_platform():
    assert "magento" in match_fingerprints("<html></html>", {"X-Magento-Vary": "abc"})
    assert "wix" in match_fingerprints("<html></html>", {"x-wix-request-id": "1"})


def test_stacked_technologies_are_all_reported():
    html = MARKUP["wix"] + MARKUP["ecwid"]
    matched = match_fingerprints(html)
    assert {"wix", "ecwid"} <= set(matched)


# ── the probe path ───────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _StubSession:
    """Answers a fixed URL→response map; everything else 404s."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.seen = []

    def get(self, url, **kw):
        self.seen.append(url)
        for pattern, response in self.mapping.items():
            if url.startswith(pattern):
                return response
        return _Resp(status=404)


def test_shopify_probe_wins_and_sets_catalog_strategy():
    session = _StubSession({
        "https://shop.example.com/products.json": _Resp(payload={"products": [{}]}),
    })
    det = detect("https://shop.example.com/collections/all", session=session)
    assert det.kind == "shopify"
    assert det.catalog_endpoint == "https://shop.example.com/products.json"
    assert det.strategy == STRATEGY_CATALOG
    assert det.has_catalog_api


def test_woocommerce_probe():
    session = _StubSession({
        "https://shop.example.com/wp-json/wc/store/products": _Resp(payload=[{}]),
    })
    det = detect("https://shop.example.com", session=session)
    assert det.kind == "woocommerce"
    assert det.strategy == STRATEGY_CATALOG


def test_fingerprint_platform_routes_to_crawl_not_catalog():
    session = _StubSession({
        "https://shop.example.com/": _Resp(text=MARKUP["bigcommerce"]),
        "https://shop.example.com": _Resp(text=MARKUP["bigcommerce"]),
    })
    det = detect("https://shop.example.com", session=session)
    assert det.kind == "bigcommerce"
    assert det.catalog_endpoint is None       # recognized, but nothing free to read
    assert det.strategy == STRATEGY_CRAWL
    assert not det.has_catalog_api


def test_client_side_platform_recommends_js():
    session = _StubSession({"https://shop.example.com": _Resp(text=MARKUP["wix"])})
    det = detect("https://shop.example.com", session=session)
    assert det.kind == "wix"
    assert det.likely_needs_js
    assert det.strategy == STRATEGY_CRAWL_JS


def test_unknown_site_falls_back_to_generic():
    session = _StubSession({"https://plain.example.com": _Resp(text="<html>hi</html>")})
    det = detect("https://plain.example.com", session=session)
    assert det.kind == "generic"
    assert det.strategy == STRATEGY_CRAWL


def test_catalog_probes_run_before_the_homepage_fetch():
    """The cheap decisive probe must come first — a Shopify store should never
    need its homepage fetched."""
    session = _StubSession({
        "https://shop.example.com/products.json": _Resp(payload={"products": [{}]}),
    })
    detect("https://shop.example.com", session=session)
    assert not any(u.rstrip("/") == "https://shop.example.com" for u in session.seen)


def test_malformed_catalog_json_does_not_crash():
    session = _StubSession({
        "https://shop.example.com/products.json": _Resp(status=200, payload=None),
        "https://shop.example.com": _Resp(text="<html>plain</html>"),
    })
    det = detect("https://shop.example.com", session=session)
    assert det.kind == "generic"


def test_anti_bot_wall_is_reported_honestly():
    """A 403 means we were refused, not that the site has no signature."""
    from scrapewright.detect import STRATEGY_BLOCKED

    session = _StubSession({"https://walled.example.com": _Resp(status=403, text="denied")})
    det = detect("https://walled.example.com", session=session)
    assert det.kind == "blocked"
    assert det.strategy == STRATEGY_BLOCKED
    assert "403" in det.note
