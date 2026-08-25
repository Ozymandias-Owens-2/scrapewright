"""API keys and usage accounting, on SQLite.

Two deliberate choices here.

**Keys are stored hashed.** The full key is shown once at creation and never
again; the database keeps only a SHA-256 digest and a short public id. A stolen
database therefore leaks no working credentials.

**Usage is metered in the three units that actually cost money** — pages
fetched, browser renders, and LLM syntheses — rather than a single opaque
"request" count. That keeps the books honest and makes the product's own
argument visible: as a customer scrapes more, `pages` climbs while `syntheses`
stays flat, because a site is compiled once and replayed for free.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

KEY_PREFIX = "sw_"
METERS = ("requests", "pages", "renders", "syntheses")

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    key_hash    TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL DEFAULT '',
    plan        TEXT NOT NULL DEFAULT 'free',
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    key_id      TEXT NOT NULL,
    day         TEXT NOT NULL,
    requests    INTEGER NOT NULL DEFAULT 0,
    pages       INTEGER NOT NULL DEFAULT 0,
    renders     INTEGER NOT NULL DEFAULT 0,
    syntheses   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, day)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApiKey:
    id: str
    label: str
    plan: str
    created_at: str
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True)
class Usage:
    """Counters for one window (a day, or a month's worth of days summed)."""

    requests: int = 0
    pages: int = 0
    renders: int = 0
    syntheses: int = 0

    def as_dict(self) -> dict[str, int]:
        return {m: getattr(self, m) for m in METERS}


class Store:
    def __init__(self, path: str | Path = "scrapewright_service.db"):
        self.path = Path(path)
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── keys ─────────────────────────────────────────────────────────────────
    def create_key(self, label: str = "", plan: str = "free") -> tuple[str, ApiKey]:
        """Mint a key. Returns ``(raw_key, record)`` — the raw key is the only
        time the caller will ever see it."""
        key_id = secrets.token_hex(4)
        raw_key = f"{KEY_PREFIX}{key_id}_{secrets.token_urlsafe(24)}"
        created = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_keys (id, key_hash, label, plan, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_id, hash_key(raw_key), label, plan, created),
            )
        return raw_key, ApiKey(id=key_id, label=label, plan=plan, created_at=created)

    def resolve(self, raw_key: str) -> ApiKey | None:
        """Look a key up by its hash. Returns None for unknown or revoked keys."""
        if not raw_key:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?", (hash_key(raw_key),)
            ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        return ApiKey(id=row["id"], label=row["label"], plan=row["plan"],
                      created_at=row["created_at"], revoked_at=row["revoked_at"])

    def revoke(self, key_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_now(), key_id),
            )
        return cur.rowcount > 0

    def list_keys(self) -> list[ApiKey]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at").fetchall()
        return [ApiKey(id=r["id"], label=r["label"], plan=r["plan"],
                       created_at=r["created_at"], revoked_at=r["revoked_at"])
                for r in rows]

    # ── usage ────────────────────────────────────────────────────────────────
    def record(self, key_id: str, *, requests: int = 0, pages: int = 0,
               renders: int = 0, syntheses: int = 0, day: str | None = None) -> None:
        day = day or date.today().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO usage (key_id, day, requests, pages, renders, syntheses) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key_id, day) DO UPDATE SET "
                "  requests = requests + excluded.requests,"
                "  pages = pages + excluded.pages,"
                "  renders = renders + excluded.renders,"
                "  syntheses = syntheses + excluded.syntheses",
                (key_id, day, requests, pages, renders, syntheses),
            )

    def usage_for_day(self, key_id: str, day: str | None = None) -> Usage:
        day = day or date.today().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM usage WHERE key_id = ? AND day = ?", (key_id, day)
            ).fetchone()
        return Usage(**{m: row[m] for m in METERS}) if row else Usage()

    def usage_for_month(self, key_id: str, month: str | None = None) -> Usage:
        month = month or date.today().strftime("%Y-%m")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT SUM(requests) r, SUM(pages) p, SUM(renders) n, SUM(syntheses) s "
                "FROM usage WHERE key_id = ? AND day LIKE ?", (key_id, f"{month}-%")
            ).fetchone()
        if row is None or row["r"] is None:
            return Usage()
        return Usage(requests=row["r"], pages=row["p"], renders=row["n"],
                     syntheses=row["s"])
