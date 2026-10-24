#!/usr/bin/env bash
# Coordinated backup of Bambu Bridge state, including spool inventory.
#
# Takes an online SQLite snapshot of every bridge database (jobs, inventory
# journal/assignments/provider identity, refill ownership, pairing devices and
# native-inbox attempt records) plus small pairing key/certificate files and the
# per-printer diagnostic history (faults and refill runs), and writes one private
# tarball with a SHA-256 manifest. Native-inbox artifact
# payloads are excluded; use the native-inbox snapshot export for those.
#
# Spoolman holds the spool quantities. Restoring bridge state without the
# matching provider state can replay or lose consumption, so when Spoolman
# runs on SQLite on this host set SPOOLMAN_DB_PATH to capture both together.
# Otherwise back up Spoolman at the same time with its own tooling.
#
# Overridable via env (defaults match config.py / bridge.env):
#   BRIDGE_DB_PATH         /var/lib/bambu-bridge/jobs.db
#   BRIDGE_PAIRING_DIR     <dir of BRIDGE_DB_PATH>/pairing
#   BRIDGE_INVENTORY_DIR   unset = inventory not configured
#   SPOOLMAN_DB_PATH       unset = Spoolman backed up separately
#   BACKUP_DIR             <dir of BRIDGE_DB_PATH>/backups
#   BACKUP_KEEP            14
set -euo pipefail
umask 077

DB="${BRIDGE_DB_PATH:-/var/lib/bambu-bridge/jobs.db}"
STATE_ROOT="$(dirname "$DB")"
PAIRING="${BRIDGE_PAIRING_DIR:-$STATE_ROOT/pairing}"
INVENTORY="${BRIDGE_INVENTORY_DIR:-}"
SPOOLMAN_DB="${SPOOLMAN_DB_PATH:-}"
BACKUP_DIR="${BACKUP_DIR:-$STATE_ROOT/backups}"
KEEP="${BACKUP_KEEP:-14}"
SMALL_FILE_BYTES=1048576

die() { echo "state-backup: $*" >&2; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 not found"
[ -f "$DB" ] || die "no jobs DB at $DB"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
work="$(mktemp -d "${TMPDIR:-/tmp}/bridge-state-$stamp.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir -p "$BACKUP_DIR"

is_sqlite() {
    # SQLite files start with this 16-byte header; WAL/SHM sidecars do not.
    # NULs are dropped before the comparison (bash would warn about them); a
    # header with a NUL in its first 15 bytes can never match anyway.
    [ "$(head -c 15 "$1" 2>/dev/null | tr -d '\0')" = "SQLite format 3" ]
}

snapshot() {  # snapshot <source file> <name inside archive>
    local source=$1 name=$2
    mkdir -p "$(dirname "$work/$name")"
    if is_sqlite "$source"; then
        # Online Backup API: consistent while the bridge is running.
        sqlite3 "$source" ".backup '$work/$name'"
    elif [ "$(stat -c %s "$source")" -le "$SMALL_FILE_BYTES" ]; then
        cp -p "$source" "$work/$name"
    else
        return 0
    fi
    printf '%s\n' "$name" >> "$work/.members"
}

snapshot "$DB" "jobs/$(basename "$DB")"

add_tree() {  # add_tree <directory> <archive prefix>
    local root=$1 prefix=$2 path relative
    [ -d "$root" ] || return 0
    while IFS= read -r -d '' path; do
        relative="${path#"$root"/}"
        case "$relative" in
            *-wal|*-shm|*-journal|*.lock|*.tmp) continue ;;
            native-inbox/*.payload) continue ;;
        esac
        snapshot "$path" "$prefix/$relative"
    done < <(find "$root" -type f -print0)
}

add_tree "$PAIRING" pairing
add_tree "$STATE_ROOT/diagnostic-history" diagnostics
if [ -n "$INVENTORY" ]; then
    [ -d "$INVENTORY" ] || die "BRIDGE_INVENTORY_DIR $INVENTORY does not exist"
    add_tree "$INVENTORY" inventory
fi
if [ -n "$SPOOLMAN_DB" ]; then
    is_sqlite "$SPOOLMAN_DB" || die "SPOOLMAN_DB_PATH is not a SQLite database"
    snapshot "$SPOOLMAN_DB" "spoolman/$(basename "$SPOOLMAN_DB")"
fi

version="$(python3 -c 'import bambu_bridge; print(bambu_bridge.__version__)' 2>/dev/null || echo unknown)"
{
    echo "created_utc=$stamp"
    echo "bridge_version=$version"
    echo "inventory_included=$([ -n "$INVENTORY" ] && echo yes || echo no)"
    echo "spoolman_included=$([ -n "$SPOOLMAN_DB" ] && echo yes || echo no)"
    echo "native_inbox_payloads=excluded"
} > "$work/BACKUP-INFO"
(cd "$work" && sort .members | xargs -d '\n' sha256sum > MANIFEST.sha256 && rm .members)

dest="$BACKUP_DIR/bridge-state-$stamp.tar.gz"
tar -C "$work" -czf "$dest.partial" .
mv "$dest.partial" "$dest"

mapfile -t old < <(find "$BACKUP_DIR" -maxdepth 1 -name 'bridge-state-*.tar.gz' -printf '%T@ %p\n' \
    | sort -rn | tail -n "+$((KEEP + 1))" | cut -d' ' -f2-)
if [ "${#old[@]}" -gt 0 ]; then
    rm -f "${old[@]}"
fi
echo "state-backup: wrote $dest ($(stat -c %s "$dest") bytes); retained $KEEP"
