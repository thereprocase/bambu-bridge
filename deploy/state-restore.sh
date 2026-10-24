#!/usr/bin/env bash
# Restore a bridge-state-*.tar.gz written by state-backup.sh.
#
# Usage: state-restore.sh ARCHIVE
#
# Run only with the bridge service stopped. The archive's manifest is verified
# before anything changes. Every current file that would be replaced is moved
# into a pre-restore-<stamp> directory beside it; nothing is deleted.
#
# Spoolman is never restored by this script. If the archive contains a
# Spoolman snapshot, restore it with Spoolman stopped at the same time, then
# start Spoolman before the bridge. The bridge detects an older provider state
# and holds inventory writes for review rather than treating it as a reconnect.
#
# Env (same defaults as state-backup.sh): BRIDGE_DB_PATH, BRIDGE_PAIRING_DIR,
# BRIDGE_INVENTORY_DIR, BRIDGE_HEALTH_URL (http://localhost:8080/api/v1/health).
set -euo pipefail
umask 077

ARCHIVE="${1:-}"
DB="${BRIDGE_DB_PATH:-/var/lib/bambu-bridge/jobs.db}"
STATE_ROOT="$(dirname "$DB")"
PAIRING="${BRIDGE_PAIRING_DIR:-$STATE_ROOT/pairing}"
INVENTORY="${BRIDGE_INVENTORY_DIR:-}"
HEALTH="${BRIDGE_HEALTH_URL:-http://localhost:8080/api/v1/health}"

die() { echo "state-restore: $*" >&2; exit 1; }
[ -f "$ARCHIVE" ] || die "usage: state-restore.sh ARCHIVE"
if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1; then
    die "the bridge is still answering at $HEALTH; stop the service first"
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
work="$(mktemp -d "${TMPDIR:-/tmp}/bridge-restore-$stamp.XXXXXX")"
trap 'rm -rf "$work"' EXIT
tar -C "$work" -xzf "$ARCHIVE"
[ -f "$work/MANIFEST.sha256" ] || die "archive has no manifest"
(cd "$work" && sha256sum --quiet -c MANIFEST.sha256) || die "manifest verification failed"

target_for() {  # map an archive member to its live path
    case "$1" in
        jobs/*) echo "$STATE_ROOT/${1#jobs/}" ;;
        pairing/*) echo "$PAIRING/${1#pairing/}" ;;
        diagnostics/*) echo "$STATE_ROOT/diagnostic-history/${1#diagnostics/}" ;;
        inventory/*)
            [ -n "$INVENTORY" ] || die "archive has inventory state; set BRIDGE_INVENTORY_DIR"
            echo "$INVENTORY/${1#inventory/}" ;;
        spoolman/*) echo "" ;;
        *) die "unexpected archive member $1" ;;
    esac
}

restored=0
while read -r _ member; do
    target="$(target_for "$member")"
    [ -n "$target" ] || continue
    if [ -e "$target" ] || [ -e "$target-wal" ]; then
        aside="$(dirname "$target")/pre-restore-$stamp"
        mkdir -p "$aside"
        for suffix in "" -wal -shm; do
            [ -e "$target$suffix" ] && mv "$target$suffix" "$aside/"
        done
    fi
    mkdir -p "$(dirname "$target")"
    cp -p "$work/$member" "$target"
    restored=$((restored + 1))
done < "$work/MANIFEST.sha256"

echo "state-restore: restored $restored files from $ARCHIVE"
echo "state-restore: previous files kept in pre-restore-$stamp directories"
if [ -d "$work/spoolman" ]; then
    echo "state-restore: this archive also holds a Spoolman snapshot; restore it with"
    echo "               Spoolman stopped before starting the bridge (not done here)."
fi
