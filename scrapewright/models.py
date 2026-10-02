"""Normalized data model shared by every extractor.

The whole point of scrapewright is that a Shopify JSON feed, a JSON-LD
``<script>`` block and a set of LLM-synthesized CSS selectors all collapse
into the *same* :class:`Product` shape, so downstream code never has to know
which source a record came from.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Fields that must be present for a record to count as "usable". The validator
# (see :mod:`scrapewright.validate`) measures how many scraped products clear
# this bar — that ratio is what decides whether a generated recipe is trusted.
CORE_FIELDS: tuple[str, ...] = ("title", "price", "url")


def parse_price(value: Any) -> Decimal | None:
    """Best-effort money parser for the messy strings real sites emit.

    Handles ``"1,250.00"`` (US), ``"1.250,00"`` (EU), ``"€1 250"`` and bare
    numbers. Rule of thumb: when both ``,`` and ``.`` appear, whichever comes
    *last* is the decimal separator and the other is a thousands separator.

    When only one kind appears, the size of the last group decides, and the
    two kinds are read the same way. **Exactly three digits after a single
    separator means thousands** -- a Dutch listing writes five thousand nine
    hundred and fifty euro as ``"€ 5.950"``, and reading that dot as a decimal
    point turned a 5,950 euro car into a 5.95 euro one on a real export.
    Anything else -- one, two, or four-plus digits -- is a fraction, because
    a thousands group is always exactly three.

    The cost of the rule is that ``"1.234 kg"`` reads as 1234. Money is what
    this parses, and in money a three-digit tail is a thousands group far more
    often than a millikilogram.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))

    # Keep only digits and separators (drops currency symbols, spaces, NBSP).
    s = re.sub(r"[^0-9.,]", "", str(value))
    if not s:
        return None

    if "," in s and "." in s:
        # The rightmost separator is the decimal point; strip the other.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        for sep in (",", "."):
            if sep not in s:
                continue
            # More than one of them can only be grouping: 1.234.567.
            if s.count(sep) > 1 or re.fullmatch(rf"\d+\{sep}\d{{3}}", s):
                s = s.replace(sep, "")
            else:
                s = s.replace(sep, ".")

    try:
        return Decimal(s)
    except InvalidOperation:
        return None


# Field names that mean money even when the caller never said `:number`.
_MONEY_NAME = re.compile(r"price|prijs|preis|prix|cost|amount|bedrag",
                         re.IGNORECASE)


def is_money_field(field) -> bool:
    """Should this field's value be read as a price?"""
    return field.kind == "number" or bool(_MONEY_NAME.search(field.name))


# A price arrives with the page's furniture attached. Whitespace between
# block elements is lost when an element's text is read, so a label in front
# ("Precio de oferta€37,00", "Onze prijs:€ 54.900"), a call to action behind
# ("€ 99.950Financieren?") or a tax note ("€ 31.850,-excl. BTW") comes out
# glued to the number -- and a glued string will not compare with the one
# stored yesterday, which is the entire job of a price watch.
_CURRENCY = r"€|\$|£|¥|₽|zł|kr|CHF|EUR|USD|GBP|PLN|SEK|NOK|DKK"
# Spaces are allowed inside a number only between thousands groups, so a
# price followed by a year does not read as one eight-digit amount.
_AMOUNT = (r"\d{1,3}(?:[   ]\d{3}(?!\d))+(?:[.,]\d{1,2})?"
           r"|\d[\d.,]*\d|\d")
_MONEY_TEXT = re.compile(
    rf"(?P<pre>(?:{_CURRENCY})\s*)?"
    rf"(?P<num>{_AMOUNT})"
    r"(?P<dash>[,.]-)?"
    rf"(?P<post>\s*(?:{_CURRENCY}))?",
    re.IGNORECASE)

# "excl. BTW", "incl. 21% btw", "zzgl. MwSt." -- part of what the price
# means and not part of the number, so it is kept, beside it.
_PRICE_NOTE = re.compile(
    r"(?:in[ck]l\.?|ex[ck]l\.?|zzgl\.?|plus|\+)\s*"
    r"(?:\d+[.,]?\d*\s*%\s*)?"
    r"(?:btw|vat|mwst|ust|iva|tva|tax|moms)\b\.?",
    re.IGNORECASE)

PRICE_ON_REQUEST = "price on request"


def clean_money(text: str) -> tuple[str | None, str | None]:
    """Separate a price from the words around it. Returns ``(value, note)``.

    ``value`` is None when the page is quoting a placeholder -- "€ 0",
    "€ 0,00". No catalogue sells a camper for nothing; that zero means
    "price on request", and reporting it as a price is reporting a wrong
    number, which is worse than reporting none.

    ``note`` carries a tax marker when the page attached one, because
    "€ 31.850 excl. BTW" and "€ 31.850" are different prices.

    Text with no number in it comes back untouched: that is a selector
    pointing at the wrong element, and swallowing it would hide the
    evidence that normally gets the selector thrown away.
    """
    raw = str(text)
    note_match = _PRICE_NOTE.search(raw)
    if note_match:
        note = " ".join(note_match.group(0).split())
        body = raw[:note_match.start()] + raw[note_match.end():]
    else:
        note, body = None, raw

    # Prefer an amount that carries a currency: on "Audi A4 2.0 TFSI €24.950"
    # the first digits on the page belong to the model name.
    best = None
    for match in _MONEY_TEXT.finditer(body):
        if best is None:
            best = match
        if match.group("pre") or match.group("post") or match.group("dash"):
            best = match
            break
    if best is None:
        return raw.strip() or None, note

    value = " ".join("".join(part for part in best.groups() if part).split())
    amount = parse_price(value)
    if amount is not None and amount == 0:
        return None, note or PRICE_ON_REQUEST
    return value, note


class Product(BaseModel):
    """One catalog item, normalized across all sources."""

    url: str
    title: str
    brand: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    available: bool | None = None
    images: list[str] = Field(default_factory=list)
    sizes: list[str] = Field(default_factory=list)
    description: str | None = None
    sku: str | None = None
    source_platform: str | None = None

    # Untouched source payload, kept for debugging. Excluded from field-coverage
    # scoring so a fat ``raw`` blob never masks a missing ``price``.
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator("price", mode="before")
    @classmethod
    def _coerce_price(cls, v: Any) -> Decimal | None:
        return parse_price(v)

    def core_fields_present(self) -> dict[str, bool]:
        """Per-core-field presence, used by the validator."""
        return {f: bool(getattr(self, f)) for f in CORE_FIELDS}

    def is_usable(self) -> bool:
        return all(self.core_fields_present().values())


class Record(BaseModel):
    """A schema-agnostic extraction result.

    :class:`Product` stays the typed shape for the built-in product schema;
    ``Record`` is what comes back for a caller-defined schema, where the field
    set is only known at runtime.
    """

    url: str
    schema_name: str = "custom"
    data: dict[str, Any] = Field(default_factory=dict)
    source_platform: str | None = None

    def get(self, field: str, default: Any = None) -> Any:
        return self.data.get(field, default)

    def to_product(self) -> Product:
        """Adapt a product-schema record into the typed model."""
        return Product(
            url=self.url,
            title=str(self.data.get("title") or ""),
            brand=self.data.get("brand"),
            price=self.data.get("price"),
            description=self.data.get("description"),
            sku=self.data.get("sku"),
            images=list(self.data.get("images") or []),
            source_platform=self.source_platform,
        )
