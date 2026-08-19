"""Deterministic WooCommerce catalog extractor via the public Store API.

WooCommerce ships a read-only Store API (``/wp-json/wc/store/products``) that
needs no auth. Prices come as integer minor units (cents) plus a
``currency_minor_unit`` divisor, which we normalize back to a decimal.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal

import requests

from ..http import get, make_session
from ..models import Product
from .base import Extractor

_TAGS = re.compile(r"<[^>]+>")
PER_PAGE = 100


def _strip_html(html: str | None) -> str | None:
    if not html:
        return None
    return _TAGS.sub("", html).strip() or None


def _price_from(prices: dict) -> Decimal | None:
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    try:
        minor = int(prices.get("currency_minor_unit", 2))
        return Decimal(int(raw)) / (Decimal(10) ** minor)
    except (ValueError, TypeError):
        return None


class WooCommerceExtractor(Extractor):
    kind = "woocommerce"
    is_catalog = True

    def __init__(self, endpoint: str, session: requests.Session | None = None,
                 max_pages: int = 20):
        self.endpoint = endpoint
        self.session = session or make_session()
        self.max_pages = max_pages

    def _product_from(self, p: dict) -> Product:
        prices = p.get("prices") or {}
        return Product(
            url=p.get("permalink", ""),
            title=p.get("name", ""),
            price=_price_from(prices),
            currency=prices.get("currency_code") or None,
            available=p.get("is_in_stock"),
            images=[img.get("src") for img in (p.get("images") or []) if img.get("src")],
            description=_strip_html(p.get("short_description") or p.get("description")),
            sku=p.get("sku") or None,
            source_platform="woocommerce",
            raw=p,
        )

    def iter_catalog(self) -> Iterator[Product]:
        for page in range(1, self.max_pages + 1):
            r = get(f"{self.endpoint}?per_page={PER_PAGE}&page={page}", session=self.session)
            if r.status_code != 200:
                break
            products = r.json() or []
            if not products:
                break
            for p in products:
                yield self._product_from(p)
            if len(products) < PER_PAGE:
                break

    def products_from_payload(self, payload: list) -> list[Product]:
        return [self._product_from(p) for p in (payload or [])]
