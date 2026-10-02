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
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from .extract.base import SelectorRecipe

DEFAULT_SCHEMA_NAME = "product"
# Where failures live inside the same file. A reserved key rather than a
# second file: one path to configure, one thing to back up.
FAILURES_KEY = "__failures__"
# How long a site stays written off before anyone pays to try it again.
FAILURE_TTL_SECONDS = 7 * 24 * 60 * 60
ONE_DAY_SECONDS = 24 * 60 * 60


def default_cache_path() -> Path:
    """Where compiled recipes live.

    Configurable because the default -- the user's home directory -- is
    ephemeral inside a container. A deployment that leaves it there throws away
    every compiled recipe on each release and pays the model to work the same
    sites out again, which is exactly the cost this cache exists to avoid.
    """
    configured = os.environ.get("SCRAPEWRIGHT_CACHE")
    if configured:
        return Path(configured)
    return Path.home() / ".scrapewright" / "recipes.json"


def domain_of(url: str) -> str:
    netloc = urlparse(url if "://" in url else f"https://{url}").netloc
    return netloc.lower().removeprefix("www.")


def cache_key(url: str, schema_name: str = DEFAULT_SCHEMA_NAME) -> str:
    domain = domain_of(url)
    return domain if schema_name == DEFAULT_SCHEMA_NAME else f"{domain}#{schema_name}"


class RecipeCache:
    """A tiny JSON-backed store of ``{key: SelectorRecipe}``."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_cache_path()

    def _load_raw(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def get(self, url: str, schema_name: str = DEFAULT_SCHEMA_NAME) -> SelectorRecipe | None:
        raw = self._load_raw().get(cache_key(url, schema_name))
        if not isinstance(raw, dict) or "fields" not in raw and raw.get("at"):
            return None
        return SelectorRecipe(**raw) if raw else None

    def put(self, url: str, recipe: SelectorRecipe,
            schema_name: str | None = None) -> None:
        schema_name = schema_name or recipe.schema_name or DEFAULT_SCHEMA_NAME
        data = self._load_raw()
        key = cache_key(url, schema_name)
        previous = data.get(key) if isinstance(data.get(key), dict) else {}
        now = time.time()
        since = previous.get("compiles_since", 0)
        fresh_day = now - since >= ONE_DAY_SECONDS
        body = recipe.model_dump()
        body["compiled_at"] = now
        # A running count, so a site that keeps being recompiled can be
        # stopped without forbidding the first honest repair of the day.
        body["compiles"] = 1 if fresh_day else int(previous.get("compiles", 0)) + 1
        body["compiles_since"] = now if fresh_day else since
        data[key] = body
        # Success retires the note that this site could not be read.
        data.get(FAILURES_KEY, {}).pop(cache_key(url, schema_name), None)
        self._write(data)

    def domains(self) -> list[str]:
        return sorted(k for k in self._load_raw() if k != FAILURES_KEY)

    # ── what did not work, and when ──────────────────────────────────────────
    def last_compiled(self, url: str,
                      schema_name: str = DEFAULT_SCHEMA_NAME) -> float | None:
        raw = self._load_raw().get(cache_key(url, schema_name))
        return raw.get("compiled_at") if isinstance(raw, dict) else None

    def compiles_today(self, url: str,
                       schema_name: str = DEFAULT_SCHEMA_NAME) -> int:
        """How many times this site has been compiled in the last day."""
        raw = self._load_raw().get(cache_key(url, schema_name))
        if not isinstance(raw, dict):
            return 0
        since = raw.get("compiles_since", 0)
        if time.time() - since >= ONE_DAY_SECONDS:
            return 0
        return int(raw.get("compiles", 0))

    def note_failure(self, url: str, schema_name: str = DEFAULT_SCHEMA_NAME,
                     reason: str = "", mode: str = "static") -> None:
        """Remember that compiling this site produced nothing usable.

        Without this, a page the model cannot read is paid for again on every
        single request. Two real examples from a day of live use: one product
        page spent three model calls and fourteen seconds to return nothing,
        every time it was read. A daily refresh of one such link quietly burns
        the customer's credits and our tokens forever.

        ``mode`` records how hard we tried. A page that defeated a plain
        fetch has not defeated a browser, and the first version of this
        forgot the difference: a static attempt wrote the site off, and the
        next request with ``js=true`` skipped the browser entirely and
        returned nothing -- on a page whose price a browser finds at once.
        """
        data = self._load_raw()
        failures = data.setdefault(FAILURES_KEY, {})
        failures[cache_key(url, schema_name)] = {"at": time.time(),
                                                 "reason": reason[:200],
                                                 "mode": mode}
        self._write(data)

    def failed_at(self, url: str,
                  schema_name: str = DEFAULT_SCHEMA_NAME) -> float | None:
        entry = self._load_raw().get(FAILURES_KEY, {}).get(
            cache_key(url, schema_name))
        return entry.get("at") if isinstance(entry, dict) else None

    def failure_mode(self, url: str,
                     schema_name: str = DEFAULT_SCHEMA_NAME) -> str | None:
        entry = self._load_raw().get(FAILURES_KEY, {}).get(
            cache_key(url, schema_name))
        return entry.get("mode", "static") if isinstance(entry, dict) else None

    def recently_failed(self, url: str, schema_name: str = DEFAULT_SCHEMA_NAME,
                        within: float = FAILURE_TTL_SECONDS,
                        *, can_js: bool = False) -> bool:
        """Has this site been written off in a way that applies to us?

        A failure recorded without a browser says nothing about a request
        that has one. Only a failure at least as strong as this attempt is
        a reason not to try.
        """
        at = self.failed_at(url, schema_name)
        if at is None or (time.time() - at) >= within:
            return False
        if can_js and self.failure_mode(url, schema_name) == "static":
            return False
        return True

    def clear_failure(self, url: str,
                      schema_name: str = DEFAULT_SCHEMA_NAME) -> None:
        data = self._load_raw()
        if data.get(FAILURES_KEY, {}).pop(cache_key(url, schema_name), None):
            self._write(data)

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")
