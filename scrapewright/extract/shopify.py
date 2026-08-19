"""Deterministic Shopify catalog extractor.

Ported from the Kazofein Antonioli scraper: Shopify exposes the whole catalog
as paginated JSON at ``/products.json``, so there is nothing to synthesize —
just map fields. Free, stable, no LLM.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import requests

from ..http import get, make_session
from ..models import Product
from .base import Extractor

_TAGS = re.compile(r"<[^>]+>")
PAGE_LIMIT = 250


def _strip_html(html: str | None) -> str | None:
    if not html:
        return None
    return _TAGS.sub("", html).strip() or None


class ShopifyExtractor(Extractor):
    kind = "shopify"
    is_catalog = True

    def __init__(self, endpoint: str, session: requests.Session | None = None,
                 max_pages: int = 20):
        self.endpoint = endpoint  # e.g. https://shop.com/products.json
        self.base = endpoint.rsplit("/products.json", 1)[0]
        self.session = session or make_session()
        self.max_pages = max_pages

    def _product_from(self, p: dict) -> Product:
        variants = p.get("variants") or []
        first = variants[0] if variants else {}
        sizes = [str(v.get("title")) for v in variants if v.get("title") and v.get("title") != "Default Title"]
        return Product(
            url=f"{self.base}/products/{p.get('handle', '')}",
            title=p.get("title", ""),
            brand=p.get("vendor") or None,
            price=first.get("price"),
            available=any(v.get("available") for v in variants) if variants else None,
            images=[img.get("src") for img in (p.get("images") or []) if img.get("src")],
            sizes=sizes,
            description=_strip_html(p.get("body_html")),
            sku=first.get("sku") or None,
            source_platform="shopify",
            raw=p,
        )

    def iter_catalog(self) -> Iterator[Product]:
        for page in range(1, self.max_pages + 1):
            r = get(f"{self.endpoint}?limit={PAGE_LIMIT}&page={page}", session=self.session)
            if r.status_code != 200:
                break
            products = r.json().get("products") or []
            if not products:
                break
            for p in products:
                yield self._product_from(p)
            if len(products) < PAGE_LIMIT:
                break

    # Also usable in page mode against a saved products.json payload (tests).
    def products_from_payload(self, payload: dict) -> list[Product]:
        return [self._product_from(p) for p in (payload.get("products") or [])]
