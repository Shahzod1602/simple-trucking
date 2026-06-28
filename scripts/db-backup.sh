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
docker exec -t "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$out"

# Prune local backups older than retention.
find "$BACKUP_DIR" -name 'SimpleTrucking-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete

# Ship off-site if configured.
if [ -n "$OFFSITE" ]; then
  echo "[$(date -Is)] shipping off-site → $OFFSITE"
  rsync -az "$out" "$OFFSITE"
fi

echo "[$(date -Is)] backup complete: $(du -h "$out" | cut -f1)"
