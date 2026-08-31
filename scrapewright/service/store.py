"""API keys and usage accounting, on SQLite.

Two deliberate choices here.

**Keys are stored hashed.** The full key is shown once at creation and never
again; the database keeps only a SHA-256 digest and a short public id. A stolen
database therefore leaks no working credentials.

**Usage separates value from cost.** `records` counts what the customer
received — the unit quotas and bills run on. `syntheses`, `renders` and `pages`
count what serving them cost us. Metering both makes the product's own argument
visible in the customer's own dashboard: records climb while syntheses stay
flat, because each site is compiled once and replayed for free.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KEY_PREFIX = "sw_"
# `records` is the unit the customer receives; the rest are what serving them
# costs us. Quotas price the first, fair-use caps protect the others.
METERS = ("requests", "pages", "renders", "syntheses", "records")

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    key_hash    TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL DEFAULT '',
    plan        TEXT NOT NULL DEFAULT 'free',
    created_at  TEXT NOT NULL,
    revoked_at  TEXT,
    -- Hashed, not stored plainly: it exists to stop one person collecting the
    -- free allowance over and over, not to build a mailing list. Stripe already
    -- holds the payer's real address for receipts.
    email_hash  TEXT
);
CREATE TABLE IF NOT EXISTS credit_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id      TEXT NOT NULL,
    delta       INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    -- Lets a grant be replayed safely: the monthly free allowance is handed
    -- out lazily on first use, and must not stack if that happens twice.
    idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS credit_ledger_key ON credit_ledger (key_id);
CREATE TABLE IF NOT EXISTS usage (
    key_id      TEXT NOT NULL,
    day         TEXT NOT NULL,
    requests    INTEGER NOT NULL DEFAULT 0,
    pages       INTEGER NOT NULL DEFAULT 0,
    renders     INTEGER NOT NULL DEFAULT 0,
    syntheses   INTEGER NOT NULL DEFAULT 0,
    records     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, day)
);
CREATE TABLE IF NOT EXISTS signups (
    -- Hashed for the same reason as the email. Only the count matters.
    ip_hash     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    -- What the address did: took a key, ran the public demo, ...
    kind        TEXT NOT NULL DEFAULT 'signup'
);
CREATE INDEX IF NOT EXISTS signups_ip ON signups (ip_hash);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def hash_identity(value: str) -> str:
    """Hash an email or IP. Case- and whitespace-insensitive, so the same
    person does not read as two."""
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


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
    records: int = 0

    def as_dict(self) -> dict[str, int]:
        return {m: getattr(self, m) for m in METERS}


class Store:
    def __init__(self, path: str | Path = "scrapewright_service.db"):
        self.path = Path(path)
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn) -> None:
        """Add meters introduced after a database was first created.

        A hosted service cannot drop its usage history to gain a column, so new
        meters arrive as nullable-with-default and backfill to zero.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(usage)")}
        for meter in METERS:
            if meter in existing:
                continue
            if not meter.isidentifier():  # names are a module constant, but the
                continue                  # interpolation below deserves a guard
            conn.execute(
                f"ALTER TABLE usage ADD COLUMN {meter} INTEGER NOT NULL DEFAULT 0")

        key_columns = {row["name"]
                       for row in conn.execute("PRAGMA table_info(api_keys)")}
        if "email_hash" not in key_columns:
            conn.execute("ALTER TABLE api_keys ADD COLUMN email_hash TEXT")

        signup_columns = {row["name"]
                          for row in conn.execute("PRAGMA table_info(signups)")}
        if signup_columns and "kind" not in signup_columns:
            conn.execute("ALTER TABLE signups ADD COLUMN kind TEXT NOT NULL "
                         "DEFAULT 'signup'")

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
    def create_key(self, label: str = "", plan: str = "free",
                   email: str | None = None) -> tuple[str, ApiKey]:
        """Mint a key. Returns ``(raw_key, record)`` — the raw key is the only
        time the caller will ever see it.

        ``email`` identifies the person behind the key. It is what stops the
        free allowance from being farmed once anyone can sign up: the grant is
        keyed to the identity, so a second key for the same address inherits
        nothing.
        """
        key_id = secrets.token_hex(4)
        raw_key = f"{KEY_PREFIX}{key_id}_{secrets.token_urlsafe(24)}"
        created = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_keys (id, key_hash, label, plan, created_at, "
                "email_hash) VALUES (?, ?, ?, ?, ?, ?)",
                (key_id, hash_key(raw_key), label, plan, created,
                 hash_identity(email) if email else None),
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

    def key_exists(self, key_id: str) -> bool:
        """Is there an account to credit? Reconciliation needs to know before
        it writes a grant nobody can ever spend."""
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM api_keys WHERE id = ?",
                                (key_id,)).fetchone() is not None

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

    def record(self, key_id: str, *, requests: int = 0, pages: int = 0,
               renders: int = 0, syntheses: int = 0, records: int = 0,
               day: str | None = None) -> None:
        day = day or date.today().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO usage (key_id, day, requests, pages, renders, "
                "                   syntheses, records) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key_id, day) DO UPDATE SET "
                "  requests = requests + excluded.requests,"
                "  pages = pages + excluded.pages,"
                "  renders = renders + excluded.renders,"
                "  syntheses = syntheses + excluded.syntheses,"
                "  records = records + excluded.records",
                (key_id, day, requests, pages, renders, syntheses, records),
            )

    # ── credits ──────────────────────────────────────────────────────────────
    def balance(self, key_id: str) -> int:
        """Credits available now: grants minus spending, as a running sum.

        A ledger rather than a counter, so every movement stays auditable and
        a disputed bill can be reconstructed line by line.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(delta), 0) AS bal FROM credit_ledger "
                "WHERE key_id = ?", (key_id,)
            ).fetchone()
        return int(row["bal"])

    def grant(self, key_id: str, amount: int, reason: str,
              idempotency_key: str | None = None) -> bool:
        """Add credits. Returns False if this exact grant was already applied."""
        if amount <= 0:
            raise ValueError("grant amount must be positive")
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO credit_ledger (key_id, delta, reason, created_at, "
                    "idempotency_key) VALUES (?, ?, ?, ?, ?)",
                    (key_id, amount, reason, _now(), idempotency_key),
                )
        except sqlite3.IntegrityError:
            return False   # replayed grant; balance already reflects it
        return True

    def spend(self, key_id: str, amount: int, reason: str) -> None:
        """Deduct credits. Recorded even if it takes the balance negative:
        work already done is charged for, and the next request is refused."""
        if amount <= 0:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO credit_ledger (key_id, delta, reason, created_at) "
                "VALUES (?, ?, ?, ?)", (key_id, -amount, reason, _now()),
            )

    def ensure_free_allowance(self, key_id: str, amount: int,
                              month: str | None = None) -> None:
        """Hand out this month's free credits, once per person per month.

        Keyed to the account's email where there is one, so minting a second
        key does not mint a second allowance. Keys made by the operator have no
        email and fall back to being their own identity -- that is deliberate:
        the abuse this guards against is self-service signup, and an operator
        with CLI access can grant themselves credits directly anyway.
        """
        month = month or date.today().strftime("%Y-%m")
        with self._conn() as conn:
            row = conn.execute("SELECT email_hash FROM api_keys WHERE id = ?",
                               (key_id,)).fetchone()
        identity = (row["email_hash"] if row and row["email_hash"] else key_id)
        self.grant(key_id, amount, f"free allowance {month}",
                   idempotency_key=f"free:{identity}:{month}")

    # ── rate limiting for the unauthenticated endpoints ──────────────────────
    def record_event(self, ip: str, kind: str = "signup") -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO signups (ip_hash, created_at, kind) "
                         "VALUES (?, ?, ?)", (hash_identity(ip), _now(), kind))

    def count_events(self, ip: str, kind: str = "signup", hours: int = 24) -> int:
        """How often this address has done ``kind`` lately."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM signups "
                "WHERE ip_hash = ? AND kind = ? AND created_at >= ?",
                (hash_identity(ip), kind, cutoff)).fetchone()
        return int(row["n"])

    # Kept: signup was the first thing that needed counting, and callers exist.
    def record_signup(self, ip: str) -> None:
        self.record_event(ip, "signup")

    def recent_signups(self, ip: str, hours: int = 24) -> int:
        return self.count_events(ip, "signup", hours)

    def grant_exists(self, idempotency_key: str) -> bool:
        """Has this exact payment already been credited?

        Exact, not a guess by description: two payments for the same pack read
        identically, and a dry run that confounds them would tell an operator
        the ledger is whole when it is missing money.
        """
        with self._conn() as conn:
            return conn.execute(
                "SELECT 1 FROM credit_ledger WHERE idempotency_key = ?",
                (idempotency_key,)).fetchone() is not None

    def ledger(self, key_id: str, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT delta, reason, created_at FROM credit_ledger "
                "WHERE key_id = ? ORDER BY id DESC LIMIT ?", (key_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── usage ────────────────────────────────────────────────────────────────
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
                "SELECT SUM(requests) r, SUM(pages) p, SUM(renders) n, "
                "       SUM(syntheses) s, SUM(records) d "
                "FROM usage WHERE key_id = ? AND day LIKE ?", (key_id, f"{month}-%")
            ).fetchone()
        if row is None or row["r"] is None:
            return Usage()
        return Usage(requests=row["r"], pages=row["p"], renders=row["n"],
                     syntheses=row["s"], records=row["d"])
