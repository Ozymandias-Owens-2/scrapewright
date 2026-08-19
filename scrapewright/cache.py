"""Persist synthesized recipes so a site is only ever "compiled" once.

The recipe cache is what makes the LLM a one-time cost. On first encounter with
a custom-HTML site, :mod:`scrapewright.pipeline` synthesizes a recipe and writes
it here keyed by domain; every later run loads it back and replays it with no
model call.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from .extract.base import SelectorRecipe


def domain_of(url: str) -> str:
    netloc = urlparse(url if "://" in url else f"https://{url}").netloc
    return netloc.lower().removeprefix("www.")


class RecipeCache:
    """A tiny JSON-backed store of ``{domain: SelectorRecipe}``."""

    def __init__(self, path: str | Path | None = None):
        default = Path.home() / ".scrapewright" / "recipes.json"
        self.path = Path(path) if path else default

    def _load_raw(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def get(self, url: str) -> SelectorRecipe | None:
        raw = self._load_raw().get(domain_of(url))
        return SelectorRecipe(**raw) if raw else None

    def put(self, url: str, recipe: SelectorRecipe) -> None:
        data = self._load_raw()
        data[domain_of(url)] = recipe.model_dump()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    def domains(self) -> list[str]:
        return sorted(self._load_raw().keys())
