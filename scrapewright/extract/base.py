"""Extractor interface + the serializable selector recipe.

Two kinds of extractor:

* **Catalog** extractors (Shopify, WooCommerce) hit a list endpoint and yield
  every product on the site. They implement :meth:`Extractor.iter_catalog`.
* **Page** extractors (JSON-LD, selector replay, LLM synthesis) turn a single
  product page's HTML into one :class:`~scrapewright.models.Product`. They
  implement :meth:`Extractor.extract_page`.

A :class:`SelectorRecipe` is the durable artifact the LLM produces once per
site: a plain dict of CSS selectors. Because it is just data, it serializes to
the cache and replays with zero LLM calls afterwards — that is the whole
cost-control story.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator

from pydantic import BaseModel, Field

from ..models import Product


class SelectorRecipe(BaseModel):
    """CSS selectors for one field each, plus how to read the value.

    ``mode`` per field: ``"text"`` (element text), ``"attr:<name>"`` (an
    attribute, e.g. ``attr:content`` or ``attr:src``), or ``"attr_all:src"``
    to collect an attribute across every match (used for image galleries).
    """

    title: str | None = None
    price: str | None = None
    brand: str | None = None
    images: str | None = None
    description: str | None = None
    sku: str | None = None
    modes: dict[str, str] = Field(default_factory=dict)
    # True when this recipe was synthesized from browser-rendered HTML, i.e. the
    # site renders its content client-side. Replaying it against a plain HTTP
    # fetch would match nothing, so the pipeline goes straight to the browser.
    needs_js: bool = False
    # Free-text note on how the recipe was derived (model name, timestamp).
    origin: str = ""

    def mode_for(self, field: str) -> str:
        return self.modes.get(field, "text")


class Extractor(abc.ABC):
    """Base class. Subclasses set :attr:`kind` and implement one of the two
    extraction methods (catalog *or* page)."""

    kind: str = "base"
    is_catalog: bool = False

    def iter_catalog(self) -> Iterator[Product]:  # pragma: no cover - overridden
        raise NotImplementedError(f"{self.kind} is not a catalog extractor")

    def extract_page(self, html: str, url: str) -> Product | None:  # pragma: no cover
        raise NotImplementedError(f"{self.kind} is not a page extractor")
