"""Replay a :class:`SelectorRecipe` against HTML — deterministically, no LLM.

This is the payoff of the whole design. Once :mod:`scrapewright.extract.llm`
has synthesized a recipe for a site (or a human has hand-written one), every
subsequent page on that site is parsed here with plain BeautifulSoup at zero
marginal cost. The LLM is a one-time compiler; this is the runtime.

The replay is schema-driven: it reads whatever fields the recipe carries, so
the same code serves the built-in product schema and any caller-defined one.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Product, Record, is_money_field
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


# Attributes that describe the markup rather than the content. A value read
# from one of these is never what a visitor sees: asked where "in stock"
# lives, a model answered the class attribute and the field came back as
# ['elementor', 'elementor-799', 'elementor-page'] on every Elementor site.
PLUMBING_ATTRS = frozenset({"class", "id", "style", "role", "rel", "target",
                            "data-id", "data-elementor-type"})


# Two prices in one string means the selector caught the container that
# holds both the old price and the sale price: fietshokje.nl returned
# "2,999,-1,899,-". Small numbers are left alone -- "incl. 21% btw" is one
# price and a tax rate, not two prices.
_MONEYISH = re.compile(r"\d[\d.,]*\d|\d")
_PRICE_FLOOR = 100


# Markup that says "this was the price before the discount". Tags first,
# then the class names shops actually use -- fietshokje.nl writes
# `.normale-prijs` beside `.sale-prijs`, with no <del> anywhere, and the two
# read as one string: "2,999,-1,899,-".
_STRUCK_TAGS = ("del", "s", "strike")
_STRUCK_CLASS = re.compile(
    r"old|was|regular|normal|normale|previous|compare|strike|"
    r"van[-_]?prijs|oude[-_]?prijs|list[-_]?price", re.IGNORECASE)


def _without_struck_prices(element):
    """A copy of this element with the before-discount price removed."""
    import copy

    clone = copy.copy(element)
    try:
        clone = BeautifulSoup(str(element), "html.parser")
    except Exception:
        return element
    for node in clone.find_all(_STRUCK_TAGS):
        node.decompose()
    for node in clone.find_all(attrs={"class": True}):
        if _STRUCK_CLASS.search(" ".join(node.get("class") or [])):
            node.decompose()
    return clone


def current_price(element) -> str | None:
    """The price a customer would pay, from an element holding two.

    A sale page shows the old price beside the new one, and a selector that
    caught their common parent reads as one string -- "2,999,-1,899,-" --
    which is not a price and is worse than nothing, because a monitoring
    customer will not notice a number that is merely wrong.

    Dropping the struck-through or "normal price" part usually leaves
    exactly one amount, and that is the answer. When it does not, this
    returns None: the field is then empty, the recipe looks incomplete, and
    something looks again rather than a wrong number being stored.
    """
    text = element.get_text(strip=True) if element is not None else ""
    if not text:
        return None
    # The markup settles it before any arithmetic does. WooCommerce writes
    # `<del>10€</del><ins>7,20€</ins>` inside one `.price`, and counting
    # amounts misses it: a 10 euro bag of coffee is under any floor a
    # "this looks like two prices" rule can safely use.
    remaining = _without_struck_prices(element).get_text(strip=True)
    if remaining and remaining != text and not holds_two_prices(remaining):
        return remaining
    if not holds_two_prices(text):
        return text
    return None


def holds_two_prices(text: str) -> bool:
    """Did this selector grab both the struck-through price and the real one?"""
    from ..models import parse_price

    amounts = []
    for token in _MONEYISH.findall(str(text)):
        value = parse_price(token)
        if value is not None and value >= _PRICE_FLOOR:
            amounts.append(value)
    return len(amounts) >= 2


def _looks_struck(element, class_depth: int = 2) -> bool:
    """Is this element the before-discount price rather than the real one?

    The tags are read all the way up -- a `<del>` anywhere above means
    everything inside it is the old price. Class names are read only on the
    element and its nearest ancestors, because `_STRUCK_CLASS` matches
    ordinary words like "normal" and a page wrapped in `<div class="normal">`
    would otherwise have no current price anywhere in it.
    """
    node, depth = element, 0
    while node is not None and getattr(node, "name", None):
        if node.name in _STRUCK_TAGS:
            return True
        if depth <= class_depth:
            names = node.get("class") if hasattr(node, "get") else None
            if names and _STRUCK_CLASS.search(" ".join(names)):
                return True
        node, depth = node.parent, depth + 1
    return False


def pick_price_element(elements):
    """The first match that is not a struck-through price.

    A WooCommerce sale page holds two `.woocommerce-Price-amount`, the first
    inside a `<del>`, and `select_one` takes the first -- so the recipe read
    250,00 for a boot selling at 199. Whether that happened at all came down
    to which selector the model wrote that day, which is a lottery, not a
    rule.

    If every match is struck the first is returned anyway: the exclusion is
    meant to choose between candidates, not to empty a field whose markup
    merely resembles a discount.
    """
    for element in elements:
        if not _looks_struck(element):
            return element
    return elements[0] if elements else None


def is_plumbing(mode: str) -> bool:
    """Does this mode read the page's wiring instead of its content?"""
    if ":" not in mode:
        return False
    return mode.split(":", 1)[1].strip().lower() in PLUMBING_ATTRS


def _natural_attr(element) -> str:
    """What a list of these elements is obviously made of."""
    name = getattr(element, "name", "")
    if name in ("img", "source"):
        return "src"
    if name == "a" and element.get("href"):
        return "href"
    return "text"


def prune_unusable(recipe, schema: Schema, html: str, url: str):
    """Drop selectors that contradict the field they claim to fill.

    A model handed a heavy page reaches for the tidy block at the top of it.
    Asked for a car's price it answered ``meta[name='description']``, whose
    content is "Occasion Volkswagen Taigo 1.0 TSI ..." -- confident, well
    formed, and not a price. Cached, it then produced that string for every
    car on the site.

    The page itself settles it: a field declared ``number`` whose selector
    finds an element with no digits in it is the wrong selector. Removing it
    leaves the field empty, which is honest, and leaves the recipe
    incomplete, which is what makes the pipeline look again rather than
    replay a lie.

    A selector that matches *nothing* is left alone. That is not evidence of
    a bad guess -- the page may not have rendered -- and discarding it cost
    a second synthesis on the very page that had just been paid for.
    """
    # On a copy: the caller's recipe is theirs. Mutating it in place quietly
    # poisoned a shared fixture across tests, and would do the same to anyone
    # holding a recipe they meant to reuse.
    recipe = recipe.model_copy(deep=True)

    # A selector reading class or id is wrong whatever it returns: those
    # describe the markup, not the content, and no visitor reads them.
    for name in list(recipe.fields):
        if is_plumbing(recipe.modes.get(name, "text")):
            recipe.fields.pop(name, None)
            recipe.modes.pop(name, None)

    values = SelectorExtractor(recipe, schema).extract_values(html, url)
    for field in schema.fields:
        if field.kind != "number" or field.name not in recipe.fields:
            continue
        value = values.get(field.name)
        # Matching nothing is not being wrong: the page may simply not have
        # rendered yet, and throwing the selector away there destroyed a good
        # one and bought a second synthesis for the same page.
        if value is None or isinstance(value, (int, float, Decimal)):
            continue
        if not any(ch.isdigit() for ch in str(value)):
            recipe.fields.pop(field.name, None)
            recipe.modes.pop(field.name, None)
    return recipe


class SelectorExtractor(Extractor):
    kind = "selector"
    is_catalog = False

    def __init__(self, recipe: SelectorRecipe, schema: Schema = PRODUCT_SCHEMA):
        self.recipe = recipe
        self.schema = schema

    def extract_values(self, html: str, url: str) -> dict[str, Any]:
        """Pull every field the recipe knows about.

        Text and lists come back as the page wrote them; fields declared
        ``number`` are parsed, with the original kept alongside. See
        :meth:`Schema.coerce`.
        """
        soup = BeautifulSoup(html, "html.parser")
        list_fields = self.schema.list_fields
        money_fields = self._money_fields()
        values: dict[str, Any] = {}

        for field in self._field_names():
            mode = self.recipe.mode_for(field)
            if is_plumbing(mode):
                continue        # class and id are wiring, never a value
            wants_many = field in list_fields or mode.startswith("attr_all:")

            for selector in self.recipe.selectors_for(field):
                if field in values:
                    break          # the better selector already answered
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
                            found.append(urljoin(url, raw) if attr in ("src", "href")
                                         else raw)
                    if found:
                        values[field] = found
                else:
                    matches = soup.select(selector)
                    element = (pick_price_element(matches)
                               if field in money_fields and mode == "text"
                               else (matches[0] if matches else None))
                    value = _read(element, mode)
                    # Two prices glued together is not a price: drop the
                    # before-discount half if the markup says which it is,
                    # and otherwise leave the field empty. Empty is honest,
                    # and it leaves the recipe looking incomplete, which is
                    # what gets it another look.
                    if value and field in money_fields and mode == "text":
                        value = current_price(element)
                    if value:
                        values[field] = value

        return self.schema.coerce(values)

    def _money_fields(self) -> frozenset[str]:
        return frozenset(f.name for f in self.schema.fields if is_money_field(f))

    def _field_names(self) -> list[str]:
        """Fields the recipe can read, including any known only as alternates."""
        names = [f for f, sel in self.recipe.fields.items() if sel]
        names.extend(f for f in self.recipe.alternates if f not in names)
        return names

    def extract_rows(self, html: str, url: str) -> list[Record]:
        """One record per card, for a recipe that names an ``item`` container.

        A listing page is not one thing, it is twenty. Read as a single record
        it gave the caller either the first card or a set of parallel lists to
        transpose by hand in a spreadsheet.
        """
        if not self.recipe.item:
            record = self.extract_record(html, url)
            return [record] if record is not None else []

        soup = BeautifulSoup(html, "html.parser")
        rows: list[Record] = []
        for card in soup.select(self.recipe.item):
            values = self._values_within(card, url)
            if values:
                rows.append(Record(url=url, schema_name=self.schema.name,
                                   data=self.schema.coerce(values),
                                   source_platform="selector"))
        return rows

    def _values_within(self, card, url: str) -> dict[str, Any]:
        """Read the recipe's fields inside one card.

        ``select`` on an element searches its descendants, which is what keeps
        a field on its own row. A selector written against the whole document
        (``article.card .price``) matches nothing *inside* a card, so each one
        is retried on its last component.
        """
        values: dict[str, Any] = {}
        money_fields = self._money_fields()
        for field in self._field_names():
            mode = self.recipe.mode_for(field)
            for selector in self.recipe.selectors_for(field):
                if field in values or selector == self.recipe.item:
                    continue
                money = field in money_fields and mode == "text"
                matches = card.select(selector)
                if not matches and " " in selector:
                    matches = card.select(selector.rsplit(" ", 1)[-1])
                el = (pick_price_element(matches) if money
                      else (matches[0] if matches else None))
                value = _read(el, mode)
                # A card on a listing shows its discount the same way a
                # product page does, and read whole it glues both prices
                # together just the same.
                if value and money:
                    value = current_price(el)
                if not value:
                    continue
                attr = mode.split(":", 1)[1] if mode.startswith("attr:") else ""
                values[field] = urljoin(url, value) if attr in ("src", "href") else value
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
