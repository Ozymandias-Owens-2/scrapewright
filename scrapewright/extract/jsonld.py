"""Page-mode extractor for schema.org/Product JSON-LD.

A large share of modern e-commerce pages embed a ``<script type="application/
ld+json">`` Product block for Google. When present it is the cleanest possible
source — structured, standardized, free — so scrapewright always tries it
before spending a single LLM token on selector synthesis.
"""

from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup

from ..models import Product
from .base import Extractor


def _iter_json_nodes(payload: Any):
    """Yield every dict inside a JSON-LD payload (handles @graph and lists)."""
    if isinstance(payload, dict):
        if "@graph" in payload and isinstance(payload["@graph"], list):
            for node in payload["@graph"]:
                yield from _iter_json_nodes(node)
        else:
            yield payload
    elif isinstance(payload, list):
        for node in payload:
            yield from _iter_json_nodes(node)


def _is_product(node: dict) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower() == "product" for x in types)


def _first_offer(node: dict) -> dict:
    offers = node.get("offers")
    if isinstance(offers, list):
        return offers[0] if offers else {}
    return offers or {}


def _as_str(v: Any) -> str | None:
    if isinstance(v, dict):
        return v.get("name") or None
    if isinstance(v, list):
        return _as_str(v[0]) if v else None
    return str(v) if v else None


def _images(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [v["url"]] if v.get("url") else []
    out: list[str] = []
    for item in v:
        out.extend(_images(item))
    return out


class JsonLdExtractor(Extractor):
    kind = "json-ld"
    is_catalog = False

    def find_product_node(self, html: str) -> dict | None:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(tag.string or tag.get_text() or "")
            except (json.JSONDecodeError, TypeError):
                continue
            for node in _iter_json_nodes(payload):
                if isinstance(node, dict) and _is_product(node):
                    return node
        return None

    def extract_page(self, html: str, url: str) -> Product | None:
        node = self.find_product_node(html)
        if node is None:
            return None
        offer = _first_offer(node)
        availability = str(offer.get("availability", "")).lower()
        return Product(
            url=node.get("url") or url,
            title=_as_str(node.get("name")) or "",
            brand=_as_str(node.get("brand")),
            price=offer.get("price") or offer.get("lowPrice"),
            currency=offer.get("priceCurrency") or None,
            available=("instock" in availability) if availability else None,
            images=_images(node.get("image")),
            description=_as_str(node.get("description")),
            sku=_as_str(node.get("sku")),
            source_platform="json-ld",
            raw=node,
        )
