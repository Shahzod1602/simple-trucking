#!/usr/bin/env bash
# SimpleTrucking — daily Postgres backup → off-site (mirrors the identify.uz db-backup pattern).
# Cron (root): 0 3 * * *  /opt/simple-trucking/scripts/db-backup.sh >> /var/log/SimpleTrucking-backup.log 2>&1
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/simple-trucking}"
DB_CONTAINER="${DB_CONTAINER:-ratecon_db}"
DB_USER="${DB_USER:-ratecon}"
DB_NAME="${DB_NAME:-ratecon}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/SimpleTrucking}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
# Off-site target (rsync/scp). Configure to your storage host, e.g.:
#   OFFSITE="storagedb:/backups/SimpleTrucking/"
OFFSITE="${OFFSITE:-}"

ts="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/SimpleTrucking-$ts.sql.gz"

echo "[$(date -Is)] dumping $DB_NAME → $out"
# No -t: a TTY would translate LF→CRLF and corrupt the binary gzip stream. With
# `set -o pipefail` a pg_dump failure aborts the script (no truncated archive kept).
docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$out"

# Integrity check: a corrupt/truncated gzip means the backup is unrestorable — fail loudly.
if ! gzip -t "$out"; then
  echo "[$(date -Is)] ERROR: gzip integrity check FAILED for $out — removing and aborting" >&2
  rm -f "$out"
  exit 1
fi

# Prune local backups older than retention.
find "$BACKUP_DIR" -name 'SimpleTrucking-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete

# Ship off-site if configured.
if [ -n "$OFFSITE" ]; then
  echo "[$(date -Is)] shipping off-site → $OFFSITE"
  if ! rsync -az "$out" "$OFFSITE"; then
    echo "[$(date -Is)] ERROR: off-site rsync FAILED → $OFFSITE (backup is local-only!)" >&2
    exit 1
  fi
else
  echo "[$(date -Is)] WARNING: OFFSITE is unset — backup is LOCAL-ONLY; a host failure loses all backups. Set OFFSITE to an off-site rsync target." >&2
fi

echo "[$(date -Is)] backup complete: $(du -h "$out" | cut -f1)"
