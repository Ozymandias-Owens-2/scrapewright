"""What to extract — declared as data, not hard-coded.

Until v0.4 the target shape was fixed: title, price, brand, images... i.e. a
product. That baked one vertical into the tool. A :class:`Schema` lifts it out:
you declare the fields you want, and the same compile-once/replay-free loop
works for any structured page — job posts, listings, papers, registry records.

The product schema is just the built-in default, defined the same way a caller
would define their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _dc_field

# How a field's value is read once its element is found.
KINDS = ("text", "number", "url", "list")


@dataclass(frozen=True)
class Field:
    """One thing to pull off the page.

    ``description`` is written for the model doing the selector synthesis —
    it is the only hint it gets about what counts as this field.
    """

    name: str
    description: str = ""
    kind: str = "text"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown field kind {self.kind!r}; use one of {KINDS}")

    @property
    def is_list(self) -> bool:
        return self.kind == "list"


@dataclass(frozen=True)
class Schema:
    """A named set of fields, plus which of them make a record worth keeping."""

    name: str
    fields: tuple[Field, ...]
    required: tuple[str, ...] = ()

    @classmethod
    def from_names(cls, names, name: str = "custom", required=None) -> Schema:
        """Build a schema from bare field names (``"price:number"`` sets a kind).

        This is what the CLI and the MCP tools accept, so a caller can ask for
        ``["title", "salary:number", "tags:list"]`` without importing anything.
        """
        fields = []
        for raw in names:
            part, _, kind = str(raw).partition(":")
            fields.append(Field(name=part.strip(), kind=(kind.strip() or "text")))
        req = tuple(required) if required else tuple(f.name for f in fields[:1])
        return cls(name=name, fields=tuple(fields), required=req)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    @property
    def list_fields(self) -> frozenset[str]:
        return frozenset(f.name for f in self.fields if f.is_list)

    def get(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    def is_satisfied_by(self, values: dict) -> bool:
        """A record is usable when every required field came back non-empty."""
        return all(values.get(name) for name in self.required)

    def prompt_lines(self) -> str:
        """The field list as the synthesis prompt should see it."""
        out = []
        for f in self.fields:
            hint = f" — {f.description}" if f.description else ""
            plural = " (selector should match every matching element)" if f.is_list else ""
            out.append(f'  "{f.name}": <css selector or null>{hint}{plural}')
        return "\n".join(out)


PRODUCT_SCHEMA = Schema(
    name="product",
    fields=(
        Field("title", "the product name", "text"),
        Field("price", "the current selling price", "number"),
        Field("brand", "the brand or maker", "text"),
        Field("images", "the product gallery images", "list"),
        Field("description", "the product description", "text"),
        Field("sku", "the product code or SKU", "text"),
    ),
    required=("title", "price"),
)

BUILTIN_SCHEMAS = {PRODUCT_SCHEMA.name: PRODUCT_SCHEMA}
