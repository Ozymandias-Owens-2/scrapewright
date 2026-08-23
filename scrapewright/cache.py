"""Persist synthesized recipes so a site is only ever "compiled" once.

The recipe cache is what makes the LLM a one-time cost. On first encounter with
a custom-HTML site, :mod:`scrapewright.pipeline` synthesizes a recipe and writes
it here; every later run loads it back and replays it with no model call.

Keys are ``domain`` for the built-in product schema and ``domain#schema`` for
any other — one site can be compiled against several field sets (products,
job posts, listings) without them overwriting each other. Product-only caches
written by earlier versions keep loading unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from .extract.base import SelectorRecipe

DEFAULT_SCHEMA_NAME = "product"


def domain_of(url: str) -> str:
    netloc = urlparse(url if "://" in url else f"https://{url}").netloc
    return netloc.lower().removeprefix("www.")


def cache_key(url: str, schema_name: str = DEFAULT_SCHEMA_NAME) -> str:
    domain = domain_of(url)
    return domain if schema_name == DEFAULT_SCHEMA_NAME else f"{domain}#{schema_name}"


class RecipeCache:
    """A tiny JSON-backed store of ``{key: SelectorRecipe}``."""

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

    def get(self, url: str, schema_name: str = DEFAULT_SCHEMA_NAME) -> SelectorRecipe | None:
        raw = self._load_raw().get(cache_key(url, schema_name))
        return SelectorRecipe(**raw) if raw else None

    def put(self, url: str, recipe: SelectorRecipe,
            schema_name: str | None = None) -> None:
        schema_name = schema_name or recipe.schema_name or DEFAULT_SCHEMA_NAME
        data = self._load_raw()
        data[cache_key(url, schema_name)] = recipe.model_dump()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    def domains(self) -> list[str]:
        return sorted(self._load_raw().keys())
