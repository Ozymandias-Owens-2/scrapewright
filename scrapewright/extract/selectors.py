"""Replay a :class:`SelectorRecipe` against HTML — deterministically, no LLM.

This is the payoff of the whole design. Once :mod:`scrapewright.extract.llm`
has synthesized a recipe for a site (or a human has hand-written one), every
subsequent page on that site is parsed here with plain BeautifulSoup at zero
marginal cost. The LLM is a one-time compiler; this is the runtime.
"""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Product
from .base import Extractor, SelectorRecipe

_TEXT_FIELDS = ("title", "price", "brand", "description", "sku")


def _read(el, mode: str) -> str | None:
    if el is None:
        return None
    if mode == "text":
        return el.get_text(strip=True) or None
    if mode.startswith("attr:"):
        return el.get(mode.split(":", 1)[1]) or None
    return None


class SelectorExtractor(Extractor):
    kind = "selector"
    is_catalog = False

    def __init__(self, recipe: SelectorRecipe):
        self.recipe = recipe

    def extract_page(self, html: str, url: str) -> Product | None:
        soup = BeautifulSoup(html, "html.parser")
        r = self.recipe
        data: dict[str, object] = {}

        for field in _TEXT_FIELDS:
            sel = getattr(r, field)
            if not sel:
                continue
            el = soup.select_one(sel)
            data[field] = _read(el, r.mode_for(field))

        if r.images:
            mode = r.mode_for("images")
            attr = mode.split(":", 1)[1] if ":" in mode else "src"
            urls = [el.get(attr) for el in soup.select(r.images) if el.get(attr)]
            data["images"] = [urljoin(url, u) for u in urls]

        if not data.get("title"):
            return None
        return Product(
            url=url,
            title=str(data.get("title") or ""),
            brand=data.get("brand"),
            price=data.get("price"),
            description=data.get("description"),
            sku=data.get("sku"),
            images=data.get("images", []),  # type: ignore[arg-type]
            source_platform="selector",
        )
