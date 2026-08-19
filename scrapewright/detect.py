"""Figure out what a store runs on before deciding how to scrape it.

The order matters: cheap, decisive probes first. A Shopify or WooCommerce site
exposes a public JSON catalog, so we never need the LLM for those — detection
alone unlocks a free, deterministic path. Only genuinely custom HTML falls
through to ``generic``, where the recipe/LLM machinery earns its keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .http import get, make_session


@dataclass
class Detection:
    kind: str  # "shopify" | "woocommerce" | "generic"
    base: str  # normalized "https://host"
    catalog_endpoint: str | None = None
    note: str = ""


def _base_of(url: str) -> str:
    p = urlparse(url if "://" in url else f"https://{url}")
    return f"{p.scheme or 'https'}://{p.netloc or p.path.split('/')[0]}"


def _looks_like_shopify(session: requests.Session, base: str) -> str | None:
    try:
        r = get(f"{base}/products.json?limit=1", session=session)
        if r.status_code == 200 and isinstance(r.json().get("products"), list):
            return f"{base}/products.json"
    except (requests.RequestException, ValueError):
        pass
    return None


def _looks_like_woocommerce(session: requests.Session, base: str) -> str | None:
    endpoint = f"{base}/wp-json/wc/store/products"
    try:
        r = get(f"{endpoint}?per_page=1", session=session)
        if r.status_code == 200 and isinstance(r.json(), list):
            return endpoint
    except (requests.RequestException, ValueError):
        pass
    return None


def detect(url: str, session: requests.Session | None = None) -> Detection:
    """Probe ``url`` and report the platform plus its catalog endpoint."""
    session = session or make_session()
    base = _base_of(url)

    shop = _looks_like_shopify(session, base)
    if shop:
        return Detection("shopify", base, shop, "products.json responded")

    woo = _looks_like_woocommerce(session, base)
    if woo:
        return Detection("woocommerce", base, woo, "wc store api responded")

    return Detection("generic", base, None, "no known catalog api; page-mode")
