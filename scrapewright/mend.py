"""Notice a field that keeps coming back empty, and learn where else it lives.

A recipe is compiled from one page, so it knows one layout. Sites have more
than one: a job board shows the salary in a pay-transparency banner on some
postings and as a line in the sidebar on others. The recipe learned the
banner, and six of twenty-five rows came back with an empty salary that was
plainly there on the page. Nobody would have noticed without checking by hand.

The trigger is the useful part. "Field is empty" means nothing on its own --
most postings really have no salary, and paying for a model call to rediscover
that would be worse than the hole. What does mean something is **empty here
and filled there**: if the same field arrives on some pages and not others,
the site is showing it two ways, and the second way can be learned.

So: hold back the incomplete records (with the HTML they came from, which is
already in hand), and once enough of them pile up next to at least one page
that did have the field, spend exactly one model call on a held page, asking
only about the fields that are missing. Whatever selector it returns is kept
as an alternate if it actually matches. The held records are then re-read --
free, the HTML never left -- and the rest of the run uses the fuller recipe.

One call per site, not per page, and only when the evidence says there is
something to find.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _dc_field
from typing import Any, Callable

from .models import Record
from .schema import Schema

# How many incomplete records to hold before spending a call. Low enough to
# fix a run early, high enough that one odd page does not trigger it.
MIN_HELD = 3
# Never hold more than this: a held record is one the caller has not been
# given yet, and memory is not free.
MAX_HELD = 8


@dataclass
class Mender:
    """Watches records go by and repairs the recipe once, if it is worth it."""

    schema: Schema
    synthesize: Callable[[str, str, Schema], Any]
    save: Callable[[Any], None]
    recipe_of: Callable[[], Any]
    reread: Callable[[Any, str, str], Record | None]

    seen_filled: set[str] = _dc_field(default_factory=set)
    held: list[tuple[Record, str, str]] = _dc_field(default_factory=list)
    spent: bool = False
    repaired: list[str] = _dc_field(default_factory=list)

    # ── watching ─────────────────────────────────────────────────────────────
    def offer(self, record: Record, html: str | None, url: str) -> Record | None:
        """Take a record. Returns it to yield now, or ``None`` if held back."""
        self.seen_filled.update(k for k, v in record.data.items() if v)
        if self.spent or html is None or len(self.held) >= MAX_HELD:
            return record
        if not self._missing(record):
            return record
        self.held.append((record, html, url))
        return None

    def _missing(self, record: Record) -> list[str]:
        """Fields this record lacks that some other record had."""
        return [name for name in self.schema.field_names
                if name in self.seen_filled and not record.data.get(name)]

    @property
    def ready(self) -> bool:
        return not self.spent and len(self.held) >= MIN_HELD

    # ── repairing ────────────────────────────────────────────────────────────
    def mend(self) -> list[Record]:
        """Spend the one call, then re-read what was held. Always empties it."""
        self.spent = True
        recipe = self.recipe_of()
        if recipe is None or not self.held:
            return self._release()

        # Ask about the page with the most holes; it shows the other layout
        # most completely.
        record, html, url = max(self.held, key=lambda h: len(self._missing(h[0])))
        wanted = self._missing(record)
        if not wanted:
            return self._release()

        narrowed = Schema(name=self.schema.name,
                          fields=tuple(f for f in self.schema.fields
                                       if f.name in wanted),
                          required=())
        try:
            found = self.synthesize(html, url, narrowed)
        except Exception:
            return self._release()
        if found is None:
            return self._release()

        # Keep only selectors that really match the page we asked about --
        # a model that guesses gets to be wrong for free otherwise.
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        changed = False
        for name, selector in (found.fields or {}).items():
            if name not in wanted or not selector:
                continue
            if soup.select_one(selector) is None:
                continue
            if recipe.add_alternate(name, selector):
                self.repaired.append(name)
                changed = True
        if changed:
            self.save(recipe)

        return self._release(reread=changed)

    def _release(self, *, reread: bool = False) -> list[Record]:
        out = []
        recipe = self.recipe_of() if reread else None
        for record, html, url in self.held:
            if reread:
                better = self.reread(recipe, html, url)
                if better is not None and better.data:
                    record = better
            out.append(record)
        self.held.clear()
        return out

    def drain(self) -> list[Record]:
        """Whatever is still held at the end of the run, unrepaired."""
        return self._release()
