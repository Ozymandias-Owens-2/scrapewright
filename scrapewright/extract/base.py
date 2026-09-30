"""Extractor interface + the serializable selector recipe.

Two kinds of extractor:

* **Catalog** extractors (Shopify, WooCommerce) hit a list endpoint and yield
  every product on the site. They implement :meth:`Extractor.iter_catalog`.
* **Page** extractors (JSON-LD, selector replay, LLM synthesis) turn a single
  page's HTML into values. They implement :meth:`Extractor.extract_page`.

A :class:`SelectorRecipe` is the durable artifact the LLM produces once per
site: a plain map of field name -> CSS selector. Because it is just data, it
serializes to the cache and replays with zero LLM calls afterwards — that is
the whole cost-control story. Since v0.4 the field set is open, so a recipe can
target any :class:`~scrapewright.schema.Schema`, not only products.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..models import Product

# Field names that were top-level attributes before recipes went schema-driven.
# Old cache files are written that way, so they are folded into `fields` on load
# and remain readable as attributes.
LEGACY_FIELDS = ("title", "price", "brand", "images", "description", "sku")


class SelectorRecipe(BaseModel):
    """CSS selectors keyed by field name, plus how to read each value.

    ``modes`` per field: ``"text"`` (element text), ``"attr:<name>"`` (an
    attribute, e.g. ``attr:content`` or ``attr:src``), or ``"attr_all:src"``
    to collect an attribute across every match.
    """

    fields: dict[str, str] = Field(default_factory=dict)
    modes: dict[str, str] = Field(default_factory=dict)
    # Where else the same field turns up. A site can show one thing in two
    # layouts -- a job board puts the salary in a pay-transparency banner on
    # some postings and in a sidebar list on others -- and a recipe compiled
    # from one page cannot see the other. Tried in order after `fields`, so
    # the selector learned from the example still wins.
    alternates: dict[str, list[str]] = Field(default_factory=dict)
    # A listing page holds one card per item. With `item` set, that selector
    # matches the cards and every field selector is read *inside* a card, so a
    # page of twenty cars yields twenty records instead of one. Empty keeps the
    # original behaviour: the whole document is a single record.
    item: str = ""
    # Which schema this recipe was synthesized against.
    schema_name: str = "product"
    # True when this recipe was synthesized from browser-rendered HTML, i.e. the
    # site renders its content client-side. Replaying it against a plain HTTP
    # fetch would match nothing, so the pipeline goes straight to the browser.
    needs_js: bool = False
    # Free-text note on how the recipe was derived (model name, timestamp).
    origin: str = ""

    @model_validator(mode="before")
    @classmethod
    def _absorb_legacy_keys(cls, data: Any) -> Any:
        """Accept (and keep reading) the pre-v0.4 flat shape."""
        if not isinstance(data, dict):
            return data
        legacy = {k: data.pop(k) for k in LEGACY_FIELDS if k in data}
        if legacy:
            merged = dict(data.get("fields") or {})
            merged.update({k: v for k, v in legacy.items() if v})
            data["fields"] = merged
        return data

    def mode_for(self, field: str) -> str:
        return self.modes.get(field, "text")

    def selector_for(self, field: str) -> str | None:
        return self.fields.get(field)

    def selectors_for(self, field: str) -> list[str]:
        """Every place to look for this field, best first."""
        primary = self.fields.get(field)
        out = [primary] if primary else []
        out.extend(s for s in self.alternates.get(field, ()) if s and s not in out)
        return out

    def add_alternate(self, field: str, selector: str) -> bool:
        """Remember another place this field lives. False if already known."""
        if not selector or selector in self.selectors_for(field):
            return False
        self.alternates.setdefault(field, []).append(selector)
        return True

    def __getattr__(self, name: str) -> Any:
        # Legacy attribute access: recipe.title, recipe.price, ...
        if name in LEGACY_FIELDS:
            return self.fields.get(name)
        raise AttributeError(name)


class Extractor(abc.ABC):
    """Base class. Subclasses set :attr:`kind` and implement one of the two
    extraction methods (catalog *or* page)."""

    kind: str = "base"
    is_catalog: bool = False

    def iter_catalog(self) -> Iterator[Product]:  # pragma: no cover - overridden
        raise NotImplementedError(f"{self.kind} is not a catalog extractor")

    def extract_page(self, html: str, url: str) -> Product | None:  # pragma: no cover
        raise NotImplementedError(f"{self.kind} is not a page extractor")
