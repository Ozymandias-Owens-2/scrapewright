"""Replay a :class:`SelectorRecipe` against HTML — deterministically, no LLM.

This is the payoff of the whole design. Once :mod:`scrapewright.extract.llm`
has synthesized a recipe for a site (or a human has hand-written one), every
subsequent page on that site is parsed here with plain BeautifulSoup at zero
marginal cost. The LLM is a one-time compiler; this is the runtime.

The replay is schema-driven: it reads whatever fields the recipe carries, so
the same code serves the built-in product schema and any caller-defined one.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Product, Record
from ..schema import PRODUCT_SCHEMA, Schema
from .base import Extractor, SelectorRecipe


def _read(el, mode: str) -> str | None:
    if el is None:
        return None
    if mode == "text":
        return el.get_text(strip=True) or None
    if mode.startswith("attr:"):
        return el.get(mode.split(":", 1)[1]) or None
    return None


def _natural_attr(element) -> str:
    """What a list of these elements is obviously made of."""
    name = getattr(element, "name", "")
    if name in ("img", "source"):
        return "src"
    if name == "a" and element.get("href"):
        return "href"
    return "text"


class SelectorExtractor(Extractor):
    kind = "selector"
    is_catalog = False

    def __init__(self, recipe: SelectorRecipe, schema: Schema = PRODUCT_SCHEMA):
        self.recipe = recipe
        self.schema = schema

    def extract_values(self, html: str, url: str) -> dict[str, Any]:
        """Pull every field the recipe knows about. Values are raw strings
        (or lists of URLs); typing is the caller's job."""
        soup = BeautifulSoup(html, "html.parser")
        list_fields = self.schema.list_fields
        values: dict[str, Any] = {}

        for field, selector in self.recipe.fields.items():
            if not selector:
                continue
            mode = self.recipe.mode_for(field)
            wants_many = field in list_fields or mode.startswith("attr_all:")

            if wants_many:
                # The mode wins when it names an attribute. Otherwise the
                # element decides: an <img> means its src, a link means its
                # href, and anything else means its text. Defaulting to src
                # regardless made list fields work for images and silently
                # return nothing for every other kind of list -- table cells,
                # tags, dates -- because a <td> has no src.
                explicit = mode.split(":", 1)[1] if ":" in mode else None
                found = []
                for el in soup.select(selector):
                    attr = explicit or _natural_attr(el)
                    raw = (el.get_text(strip=True) if attr == "text"
                           else el.get(attr))
                    if raw:
                        found.append(urljoin(url, raw) if attr in ("src", "href") else raw)
                if found:
                    values[field] = found
            else:
                value = _read(soup.select_one(selector), mode)
                if value:
                    values[field] = value

        return values

    def extract_record(self, html: str, url: str) -> Record | None:
        values = self.extract_values(html, url)
        if not self.schema.is_satisfied_by(values):
            # Return what we found anyway when *something* landed — the caller
            # decides whether a partial record is worth keeping.
            if not values:
                return None
        return Record(url=url, schema_name=self.schema.name, data=values,
                      source_platform="selector")

    def extract_page(self, html: str, url: str) -> Product | None:
        """Product-schema adapter, kept for the typed path."""
        record = self.extract_record(html, url)
        if record is None or not record.data.get("title"):
            return None
        return record.to_product()
