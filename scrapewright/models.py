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
    elif "," in s:
        # Single comma followed by exactly 2 digits → decimal, else thousands.
        s = s.replace(",", ".") if re.fullmatch(r"\d+,\d{2}", s) else s.replace(",", "")

    try:
        return Decimal(s)
    except InvalidOperation:
        return None


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
