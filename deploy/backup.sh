#!/usr/bin/env bash
# SQLite online backup of the Bambu Bridge DB.
#
# The bridge runs the DB in WAL mode with a single long-lived connection
# (db/jobs.py). `sqlite3 ".backup"` uses the Online Backup API, so this is
# safe to run *while the service is up* — it produces one consistent file
# (no -wal/-shm to ship alongside). Run from cron or the bundled systemd
# timer (deploy/bambu-bridge-backup.timer).
#
# Overridable via env (defaults match config.py / bridge.env):
#   BRIDGE_DB_PATH   source DB           (/var/lib/bambu-bridge/jobs.db)
#   BACKUP_DIR       destination dir     (/var/lib/bambu-bridge/backups)
#   BACKUP_KEEP      how many to retain  (14)
set -euo pipefail

DB="${BRIDGE_DB_PATH:-$HOME/.local/share/bambu-bridge/jobs.db}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.local/share/bambu-bridge/backups}"
KEEP="${BACKUP_KEEP:-14}"

command -v sqlite3 >/dev/null 2>&1 || { echo "backup: sqlite3 not found" >&2; exit 1; }
[ -f "$DB" ] || { echo "backup: no DB at $DB (nothing to back up yet)" >&2; exit 0; }

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
tmp="$BACKUP_DIR/.jobs-$stamp.db.partial"
dest="$BACKUP_DIR/jobs-$stamp.db.gz"

# .backup to a temp file, gzip, atomically rename — a reader never sees a
# half-written backup, and a crash mid-run leaves only a .partial to sweep.
sqlite3 "$DB" ".backup '$tmp'"
gzip -c "$tmp" > "$dest.partial"
mv "$dest.partial" "$dest"
rm -f "$tmp"

# Retain the newest $KEEP, delete the rest. Also sweep stale .partial files.
find "$BACKUP_DIR" -maxdepth 1 -name '.jobs-*.db.partial' -mmin +60 -delete 2>/dev/null || true
mapfile -t old < <(ls -1t "$BACKUP_DIR"/jobs-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))")
if [ "${#old[@]}" -gt 0 ]; then
    rm -f "${old[@]}"
fi

echo "backup: wrote $dest ($(du -h "$dest" | cut -f1)); retained $KEEP"
