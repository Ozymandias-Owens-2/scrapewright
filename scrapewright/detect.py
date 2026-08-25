"""Figure out what a site runs on before deciding how to scrape it.

Two questions, answered in order of cost:

1. **Is there a free catalog API?** Shopify and WooCommerce publish one, so a
   single cheap probe unlocks a fully deterministic path — no LLM, no browser.
2. **If not, what is this?** One homepage fetch, matched against platform
   fingerprints. Naming the platform is not cosmetic: it tells the caller which
   strategy to use and warns when a site is likely to need a rendered browser.

Note what this module deliberately does *not* do: ship a bespoke extractor per
platform. The recipe path already parses arbitrary HTML, so BigCommerce,
Magento, Wix and the rest are handled by the same compile-once/replay-free loop
as any custom site. Detection's job is routing, not parsing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from .http import get, make_session

# Strategies a caller can act on.
STRATEGY_CATALOG = "catalog"   # free platform API; deterministic
STRATEGY_CRAWL = "crawl"       # frontier + recipe path
STRATEGY_CRAWL_JS = "crawl+js"  # ...and it will probably need a browser
STRATEGY_BLOCKED = "blocked"   # anti-bot wall; deliberately not fought

# Statuses that mean "we were refused", not "nothing is here".
BLOCKED_STATUSES = frozenset({401, 403, 429})


@dataclass(frozen=True)
class Platform:
    """One platform we can recognize.

    ``catalog_path``/``validator`` describe a free JSON catalog, when the
    platform has one. ``fingerprints`` are substrings that identify the
    platform from its homepage HTML or response headers.
    """

    name: str
    catalog_path: str | None = None
    validator: Callable[[Any], bool] | None = None
    catalog_endpoint_path: str | None = None
    fingerprints: tuple[str, ...] = ()
    header_fingerprints: tuple[str, ...] = ()
    # Platforms that assemble their catalog in the browser. The runtime
    # escalation catches this anyway; flagging it up front saves a wasted pass.
    client_side_rendered: bool = False
    note: str = ""


def _valid_shopify(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("products"), list)


def _valid_woocommerce(payload: Any) -> bool:
    return isinstance(payload, list)


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        name="shopify",
        catalog_path="/products.json?limit=1",
        catalog_endpoint_path="/products.json",
        validator=_valid_shopify,
        fingerprints=("cdn.shopify.com", "Shopify.theme", "shopify-features"),
        header_fingerprints=("x-shopify-stage", "x-shopid"),
        note="products.json responded",
    ),
    Platform(
        name="woocommerce",
        catalog_path="/wp-json/wc/store/products?per_page=1",
        catalog_endpoint_path="/wp-json/wc/store/products",
        validator=_valid_woocommerce,
        fingerprints=("woocommerce", "wp-content/plugins/woocommerce"),
        note="wc store api responded",
    ),
    Platform(
        name="magento",
        fingerprints=("magento_", "/static/version", "mage/cookies",
                      "data-role=\"main-css-loader\""),
        header_fingerprints=("x-magento-vary", "x-magento-cache-debug"),
        note="Magento/Adobe Commerce markup",
    ),
    Platform(
        name="bigcommerce",
        fingerprints=("cdn11.bigcommerce.com", "bigcommerce.com/s-",
                      "stencil-utils"),
        note="BigCommerce (Stencil) markup",
    ),
    Platform(
        name="salesforce-commerce",
        fingerprints=("/on/demandware.store/", "demandware.static", "dwvar_"),
        note="Salesforce Commerce Cloud (Demandware) URLs",
    ),
    Platform(
        name="squarespace",
        fingerprints=("static1.squarespace.com", "squarespace-cdn.com",
                      "squarespace.afterbodyload", "sqs-block"),
        note="Squarespace markup",
    ),
    Platform(
        name="wix",
        fingerprints=("wixstatic.com", "wix-warmup-data", "_wixcidx",
                      "wixsite.com"),
        header_fingerprints=("x-wix-request-id",),
        client_side_rendered=True,
        note="Wix markup (renders client-side)",
    ),
    Platform(
        name="webflow",
        fingerprints=("data-wf-site", "webflow.js", "assets.website-files.com",
                      "cdn.prod.website-files.com"),
        note="Webflow markup",
    ),
    Platform(
        name="prestashop",
        fingerprints=("prestashop", "/modules/ps_", "js/jquery/plugins"),
        note="PrestaShop markup",
    ),
    Platform(
        name="shopware",
        fingerprints=("/bundles/storefront/", "shopware.config",
                      "csrf/generate"),
        note="Shopware markup",
    ),
    Platform(
        name="ecwid",
        fingerprints=("app.ecwid.com", "ecwid.com/script.js", "ecwid_"),
        client_side_rendered=True,
        note="Ecwid widget (renders client-side)",
    ),
    Platform(
        name="opencart",
        fingerprints=("catalog/view/theme", "index.php?route=product"),
        note="OpenCart markup",
    ),
)

PLATFORMS_BY_NAME = {p.name: p for p in PLATFORMS}
CATALOG_PLATFORMS = tuple(p for p in PLATFORMS if p.catalog_path)


@dataclass
class Detection:
    kind: str  # a platform name, or "generic"
    base: str  # normalized "https://host"
    catalog_endpoint: str | None = None
    note: str = ""
    strategy: str = STRATEGY_CRAWL
    likely_needs_js: bool = False
    # Every platform we matched, strongest first — sites stack technologies
    # (a Wix storefront running Ecwid, say), so this is not always one answer.
    matched: list[str] = field(default_factory=list)

    @property
    def has_catalog_api(self) -> bool:
        return self.catalog_endpoint is not None


def _base_of(url: str) -> str:
    p = urlparse(url if "://" in url else f"https://{url}")
    return f"{p.scheme or 'https'}://{p.netloc or p.path.split('/')[0]}"


def _probe_catalog(session: requests.Session, base: str,
                   platform: Platform) -> str | None:
    """Ask a platform's public catalog endpoint whether it is home."""
    try:
        r = get(f"{base}{platform.catalog_path}", session=session)
        if r.status_code == 200 and platform.validator(r.json()):
            return f"{base}{platform.catalog_endpoint_path}"
    except (requests.RequestException, ValueError):
        pass
    return None


def match_fingerprints(html: str, headers: dict | None = None) -> list[str]:
    """Platform names whose fingerprints appear in this page. Pure — no I/O,
    so it is unit-testable against saved HTML."""
    haystack = (html or "").lower()
    header_blob = " ".join(f"{k}:{v}" for k, v in (headers or {}).items()).lower()
    hits = []
    for platform in PLATFORMS:
        if any(f.lower() in haystack for f in platform.fingerprints):
            hits.append(platform.name)
        elif any(f.lower() in header_blob for f in platform.header_fingerprints):
            hits.append(platform.name)
    return hits


def detect(url: str, session: requests.Session | None = None) -> Detection:
    """Probe ``url`` and report the platform, its catalog endpoint, and the
    strategy that fits."""
    session = session or make_session()
    base = _base_of(url)

    # 1) Free catalog APIs — decisive, and worth two cheap requests.
    for platform in CATALOG_PLATFORMS:
        endpoint = _probe_catalog(session, base, platform)
        if endpoint:
            return Detection(kind=platform.name, base=base,
                             catalog_endpoint=endpoint, note=platform.note,
                             strategy=STRATEGY_CATALOG, matched=[platform.name])

    # 2) One homepage fetch, matched against every fingerprint we know.
    html, headers = "", {}
    try:
        r = get(base, session=session)
        html = r.text or ""
        # `session` is a documented injection point, so tolerate duck-typed
        # responses that carry a body but no headers.
        headers = dict(getattr(r, "headers", None) or {})
        if r.status_code in BLOCKED_STATUSES:
            # Say so plainly. "No signature found" would be a lie — we never
            # got to look, and no strategy will fix a challenge page.
            return Detection(kind="blocked", base=base,
                             note=f"blocked: HTTP {r.status_code} "
                                  "(anti-bot challenge; out of scope)",
                             strategy=STRATEGY_BLOCKED)
    except requests.RequestException as e:
        return Detection(kind="generic", base=base,
                         note=f"unreachable: {e}", strategy=STRATEGY_CRAWL)

    matched = match_fingerprints(html, headers)
    if not matched:
        return Detection(kind="generic", base=base,
                         note="no known platform signature; recipe path",
                         strategy=STRATEGY_CRAWL)

    primary = PLATFORMS_BY_NAME[matched[0]]
    needs_js = any(PLATFORMS_BY_NAME[m].client_side_rendered for m in matched)
    return Detection(
        kind=primary.name,
        base=base,
        catalog_endpoint=None,   # recognized, but no free catalog to read
        note=primary.note,
        strategy=STRATEGY_CRAWL_JS if needs_js else STRATEGY_CRAWL,
        likely_needs_js=needs_js,
        matched=matched,
    )
