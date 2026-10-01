# Restoring the database

Written down because a backup nobody has restored from is not a backup, and
because whoever needs this will be having a bad day and should not have to
think.

Rehearsed on 1 October 2026: 15 keys and 36 ledger rows came back identical,
`pragma integrity_check` clean.

## What is replicated, and what is not

Litestream ships `/data/service.db` — keys, usage, the credit ledger — to the
Tigris bucket `scrapewright-data` as it is written. Recovery point is
seconds.

The recipe cache (`/data/recipes.json`) is **not** replicated: it is a JSON
file, not SQLite. Recipes cost money to compile but can be compiled again,
and losing them locks nobody out.

## Normal case: a new or blank volume

Nothing to do. The entrypoint runs `litestream restore -if-db-not-exists
-if-replica-exists` on every boot, so a volume that comes back empty is
refilled before the service starts. Confirm afterwards:

    flyctl ssh console -a scrapewright-api -C "sh -c 'litestream snapshots -config /etc/litestream.yml \$SCRAPEWRIGHT_DB'"

## The database is corrupt, or something was deleted by mistake

Restoring over a live database is the one dangerous move here, so take a copy
first and look at it before replacing anything.

    # 1. Restore beside the live file, not over it
    flyctl ssh console -a scrapewright-api -C "sh -c 'litestream restore -config /etc/litestream.yml -o /tmp/check.db \$SCRAPEWRIGHT_DB'"

    # 2. Look at what came back
    flyctl ssh console -a scrapewright-api -C "python -c \"import sqlite3;c=sqlite3.connect('/tmp/check.db');print(c.execute('pragma integrity_check').fetchone());print(c.execute('select count(*) from api_keys').fetchone())\""

    # 3. Only if it looks right: stop the service, swap, start it
    flyctl machine stop <machine-id> -a scrapewright-api
    flyctl ssh console -a scrapewright-api -C "sh -c 'mv \$SCRAPEWRIGHT_DB \$SCRAPEWRIGHT_DB.broken && mv /tmp/check.db \$SCRAPEWRIGHT_DB'"
    flyctl machine start <machine-id> -a scrapewright-api

Keep `.broken` until the service has run for a day. It costs nothing and it
is the only copy of whatever the replica missed.

## Restoring to a point in the past

    litestream restore -config /etc/litestream.yml -timestamp 2026-10-01T12:00:00Z -o /tmp/at-noon.db $SCRAPEWRIGHT_DB

Retention is 72 hours, so anything older than three days is gone.

## If the ledger is wrong but the keys are fine

Do not restore. Payments live in Stripe, which is the real record, and the
ledger can be rebuilt from it:

    flyctl ssh console -a scrapewright-api -C "scrapewright credits reconcile --since-days 30 --dry-run"

Drop `--dry-run` once the output looks right.

## If the replica itself is gone

The keys are unrecoverable — only their hashes were ever stored. What can be
done: `credits reconcile` rebuilds balances from Stripe history, and each
customer's purchase metadata carries a hashed email, so a new key can be
issued and the old balance moved onto it by hand. Tell people plainly; that
is a worse day than it sounds and pretending otherwise makes it worse still.
