#!/bin/sh
# Start the service under Litestream, restoring first if the disk is empty.
#
# The restore is the whole point of the arrangement and it has to be
# automatic: a volume that comes back blank after a host failure would
# otherwise start a brand-new database, hand out new keys, and leave every
# customer locked out with no sign that anything was lost.
#
# `-if-db-not-exists` means an existing database is never overwritten, and
# `-if-replica-exists` means a first deploy with an empty bucket is not an
# error. Both flags make this safe to run on every boot.
set -e

if [ -n "$BUCKET_NAME" ]; then
  litestream restore -if-db-not-exists -if-replica-exists -config /etc/litestream.yml \
    "$SCRAPEWRIGHT_DB" || echo "litestream: nothing to restore, starting fresh"
  exec litestream replicate -config /etc/litestream.yml -exec "$*"
fi

# No bucket configured -- a self-hosted or local run. Replication is a
# deployment choice, not a requirement, so the service still starts.
echo "litestream: no BUCKET_NAME, running without replication"
exec "$@"
