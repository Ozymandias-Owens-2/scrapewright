"""Measure how well a batch of extracted products turned out.

A recipe is only trusted once its output clears a coverage bar. The validator
reports, across a sample of products, what fraction carry each core field and
what fraction are fully usable — the single number the pipeline thresholds on
before caching a synthesized recipe or accepting a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import CORE_FIELDS, Product


@dataclass
class Coverage:
    total: int
    usable: int
    per_field: dict[str, float] = field(default_factory=dict)

    @property
    def usable_ratio(self) -> float:
        return self.usable / self.total if self.total else 0.0

    def meets(self, threshold: float) -> bool:
        return self.usable_ratio >= threshold


def coverage(products: list[Product]) -> Coverage:
    """Per-field fill rate and overall usable ratio for a batch."""
    n = len(products)
    if n == 0:
        return Coverage(total=0, usable=0, per_field={f: 0.0 for f in CORE_FIELDS})

    per_field = {f: 0 for f in CORE_FIELDS}
    usable = 0
    for p in products:
        present = p.core_fields_present()
        for f, ok in present.items():
            per_field[f] += int(ok)
        if all(present.values()):
            usable += 1

    return Coverage(
        total=n,
        usable=usable,
        per_field={f: round(c / n, 3) for f, c in per_field.items()},
    )
