"""Keep the compiled recipes somewhere a lost volume cannot take them.

Litestream replicates the SQLite database to Tigris and restores it on boot.
It replicates SQLite and nothing else, and the recipe cache is a JSON file
sitting beside it on the same volume -- 58 recipes with no copy anywhere, each
one 300 credits to work out again, and the volume is a single machine's disk.

So the file rides along inside the database. Nothing new to configure, no
second set of credentials, no second thing that can be misconfigured in
silence, and the restore is already written and already tested every time the
machine boots.

Deliberately not part of :class:`~scrapewright.cache.RecipeCache`: the library
has no database and should not grow one. This is the hosted service taking
care of its own disk.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# How often the file is checked for changes. A compile is rare and the loss
# window is at most this long, so the exposure is a recipe or two -- against a
# write of the whole file every minute if we made it any keener.
INTERVAL_SECONDS = 60.0


def _readable(path: Path) -> str | None:
    """The file's contents, if it is a JSON object worth keeping."""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return body if isinstance(parsed, dict) and parsed else None


def restore_if_missing(store, path: str | os.PathLike) -> bool:
    """Write the saved recipes back, but only over nothing.

    A machine that still has its volume has the better copy: it may hold
    compilations newer than the last snapshot. This only fills a gap -- a
    fresh volume, a restored one, a file truncated by a torn write.
    """
    path = Path(path)
    if _readable(path) is not None:
        return False
    body = store.load_recipes()
    if not body:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".restore")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    log.warning("recipes: restored %d bytes from the database", len(body))
    return True


def back_up(store, path: str | os.PathLike) -> bool:
    """Snapshot the file into the database if it says something new."""
    body = _readable(Path(path))
    if body is None or body == store.load_recipes():
        return False
    store.save_recipes(body)
    return True


def watch(store, path: str | os.PathLike,
          interval: float = INTERVAL_SECONDS) -> threading.Thread:
    """Back the file up on a timer, for as long as the process lives."""
    def loop() -> None:
        while True:
            # Snapshot first, then wait. Sleeping first left a minute after
            # every boot with nothing saved, and a machine that crashes
            # inside that minute, every time, never saves anything at all.
            try:
                back_up(store, path)
            except Exception:            # a backup must never take the service down
                log.exception("recipes: backup failed")
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="recipe-backup", daemon=True)
    thread.start()
    return thread
