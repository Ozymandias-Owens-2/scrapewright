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
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from .extract.base import SelectorRecipe

DEFAULT_SCHEMA_NAME = "product"
# Where failures live inside the same file. A reserved key rather than a
# second file: one path to configure, one thing to back up.
FAILURES_KEY = "__failures__"
# Compilations counted here rather than on the recipe, because the ones worth
# counting most are the ones that produced no recipe to count them on.
COMPILES_KEY = "__compiles__"
# How long a site stays written off before anyone pays to try it again.
FAILURE_TTL_SECONDS = 7 * 24 * 60 * 60
ONE_DAY_SECONDS = 24 * 60 * 60
# One writer at a time. Four rows of a spreadsheet now arrive together, and
# read-modify-write without this drops whichever change was read first: a
# compile counted by one thread and forgotten by the next, which is how a
# domain compiled three times under a ceiling of two.
# ponytail: a thread lock, because the service is one process on one machine.
# A second process needs a file lock, and a second machine needs the store.
_WRITE_LOCK = threading.RLock()


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


def failure_key(url: str, schema_name: str = DEFAULT_SCHEMA_NAME) -> str:
    """Where "this page gave nothing" is filed -- per page, not per site.

    A recipe is worth sharing across a domain; a verdict that a page reads
    as nothing is not. One live spreadsheet watches two cosmos-cafe.es
    products, one readable and one not. Filed by domain, the readable one's
    success retired the other's note on every single refresh, and the
    unreadable page bought a fresh compilation for ever -- 310 credits a
    run, with nothing to show for any of them.
    """
    return url.split('#', 1)[0] + '|' + schema_name


class RecipeCache:
    """A tiny JSON-backed store of ``{key: SelectorRecipe}``."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_cache_path()

    @contextmanager
    def _mutate(self):
        """Read, change and write back with nobody else in between."""
        with _WRITE_LOCK:
            data = self._load_raw()
            yield data
            self._write(data)

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
        with self._mutate() as data:
            self._put(data, url, recipe, schema_name)

    def _put(self, data: dict, url: str, recipe: SelectorRecipe,
             schema_name: str) -> None:
        key = cache_key(url, schema_name)
        body = recipe.model_dump()
        body["compiled_at"] = time.time()
        data[key] = body
        # Success retires the note that this page could not be read. Only
        # this page: the next product along may still be unreadable.
        data.get(FAILURES_KEY, {}).pop(failure_key(url, schema_name), None)

    def domains(self) -> list[str]:
        return sorted(k for k in self._load_raw()
                      if k not in (FAILURES_KEY, COMPILES_KEY))

    # ── what did not work, and when ──────────────────────────────────────────
    def last_compiled(self, url: str,
                      schema_name: str = DEFAULT_SCHEMA_NAME) -> float | None:
        raw = self._load_raw().get(cache_key(url, schema_name))
        return raw.get("compiled_at") if isinstance(raw, dict) else None

    def note_compile(self, url: str,
                     schema_name: str = DEFAULT_SCHEMA_NAME) -> None:
        """Record that the model was paid to read this site.

        Counted on the way in, not on a recipe written on the way out. A
        synthesis that produces nothing usable writes no recipe, so counting
        it there meant the daily ceiling never bound on exactly the pages it
        exists to cap: five such URLs in one spreadsheet re-compiled on every
        refresh and were 96% of the bill.
        """
        with self._mutate() as data:
            counts = data.setdefault(COMPILES_KEY, {})
            key = cache_key(url, schema_name)
            entry = counts.get(key) if isinstance(counts.get(key), dict) else {}
            now = time.time()
            since = entry.get("since", 0)
            fresh_day = now - since >= ONE_DAY_SECONDS
            counts[key] = {"n": 1 if fresh_day else int(entry.get("n", 0)) + 1,
                           "since": now if fresh_day else since}

    def compiles_today(self, url: str,
                       schema_name: str = DEFAULT_SCHEMA_NAME) -> int:
        """How many times this site has been compiled in the last day."""
        entry = self._load_raw().get(COMPILES_KEY, {}).get(
            cache_key(url, schema_name))
        if not isinstance(entry, dict):
            return 0
        if time.time() - entry.get("since", 0) >= ONE_DAY_SECONDS:
            return 0
        return int(entry.get("n", 0))

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
        with self._mutate() as data:
            data.setdefault(FAILURES_KEY, {})[failure_key(url, schema_name)] = {
                "at": time.time(), "reason": reason[:200], "mode": mode}

    def failed_at(self, url: str,
                  schema_name: str = DEFAULT_SCHEMA_NAME) -> float | None:
        entry = self._load_raw().get(FAILURES_KEY, {}).get(
            failure_key(url, schema_name))
        return entry.get("at") if isinstance(entry, dict) else None

    def failure_mode(self, url: str,
                     schema_name: str = DEFAULT_SCHEMA_NAME) -> str | None:
        entry = self._load_raw().get(FAILURES_KEY, {}).get(
            failure_key(url, schema_name))
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
        with self._mutate() as data:
            data.get(FAILURES_KEY, {}).pop(failure_key(url, schema_name), None)

    def _write(self, data: dict) -> None:
        # Written beside the real file and moved into place, because a write
        # cut short leaves invalid JSON and _load_raw reads invalid JSON as an
        # empty cache -- every recipe on the machine, silently gone, and paid
        # for again one page at a time.
        # The temporary name carries the thread as well as the process: two
        # threads sharing one name is not a near miss but a guaranteed
        # collision, and on Windows the loser's os.replace raises outright.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(
            f"{self.path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)
