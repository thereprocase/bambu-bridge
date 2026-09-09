#!/usr/bin/env bash
# bambu-bridge-update.sh — safely update a running Bambu Bridge installation.
#
# Usage:
#   bash deploy/bambu-bridge-update.sh [--prefix DIR] [--no-restart] [--help]
#
# Options:
#   --prefix DIR   Installation directory (default: ~/bambu-bridge).
#   --no-restart   Update code but do not restart or wait for the service.
#   --help         Print this message and exit.
#
# What this script does, in order:
#   1. Backs up the SQLite job database (online, safe while the service is up).
#   2. Updates the source code — git pull (if a checkout) or installs a wheel.
#   3. Runs pip install to bring the package up to date.
#   4. Optionally restarts the service and waits for /api/v1/health to return ok.
#   5. Prints old and new version so you can confirm the update landed.
#
# Idempotent: running it twice in a row is safe.
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PREFIX="${HOME}/bambu-bridge"
NO_RESTART=0
WHEEL=""          # set by --wheel PATH

# Paths derived from the standard deploy layout (deploy/DEPLOY.md, config.py).
DB_PATH="${BRIDGE_DB_PATH:-${HOME}/.local/share/bambu-bridge/jobs.db}"
BACKUP_DIR="${BACKUP_DIR:-${HOME}/.local/share/bambu-bridge/backups}"

# Health endpoint — no auth required (contract §1 / DEPLOY.md).
HEALTH_ENDPOINT="http://localhost:8080/api/v1/health"
VERSION_ENDPOINT="http://localhost:8080/api/v1/version"
HEALTH_WAIT_SECS=30   # how long to poll /health after restart
HEALTH_POLL_SECS=2    # interval between polls

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '[update] %s\n' "$*"; }
warn() { printf '[update] WARNING: %s\n' "$*" >&2; }
die()  { printf '[update] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    grep '^#' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

# Best-effort version fetch — returns the empty string on any failure.
fetch_version() {
    curl -fsS --max-time 5 "${VERSION_ENDPOINT}" 2>/dev/null \
        | sed -n 's/.*"version"\s*:\s*"\([^"]*\)".*/\1/p' \
        || true
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            [[ -n "${2:-}" ]] || die "--prefix requires an argument."
            PREFIX="$2"; shift 2 ;;
        --prefix=*)
            PREFIX="${1#--prefix=}"; shift ;;
        --no-restart)
            NO_RESTART=1; shift ;;
        --wheel)
            [[ -n "${2:-}" ]] || die "--wheel requires a path argument."
            WHEEL="$2"; shift 2 ;;
        --wheel=*)
            WHEEL="${1#--wheel=}"; shift ;;
        --help|-h)
            usage ;;
        *)
            die "Unknown option: $1  (try --help)" ;;
    esac
done

# ---------------------------------------------------------------------------
# Validate prefix
# ---------------------------------------------------------------------------
[[ -d "${PREFIX}" ]] || die "Installation directory '${PREFIX}' does not exist. Check --prefix."

VENV="${PREFIX}/.venv"
[[ -x "${VENV}/bin/pip" ]] || die "No virtualenv at '${VENV}'. Has the bridge been installed? See deploy/DEPLOY.md."

# ---------------------------------------------------------------------------
# Step 0 — record the current version (best-effort; bridge may be stopped)
# ---------------------------------------------------------------------------
log "Checking current version..."
OLD_VERSION="$(fetch_version)"
if [[ -n "${OLD_VERSION}" ]]; then
    log "Current bridge version: ${OLD_VERSION}"
else
    log "Bridge is not responding on ${VERSION_ENDPOINT} — version unknown (may already be stopped)."
fi

# ---------------------------------------------------------------------------
# Step 1 — back up the database
# ---------------------------------------------------------------------------
log "Backing up the job database..."

if [[ ! -f "${DB_PATH}" ]]; then
    log "No database found at ${DB_PATH} — nothing to back up yet (fresh install)."
else
    if ! command -v sqlite3 >/dev/null 2>&1; then
        warn "sqlite3 not found — falling back to plain 'cp' for the backup."
        mkdir -p "${BACKUP_DIR}"
        STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
        BACKUP_DEST="${BACKUP_DIR}/jobs-pre-update-${STAMP}.db"
        cp "${DB_PATH}" "${BACKUP_DEST}"
        log "Database backed up (cp) to: ${BACKUP_DEST}"
    else
        mkdir -p "${BACKUP_DIR}"
        STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
        BACKUP_TMP="${BACKUP_DIR}/.jobs-pre-update-${STAMP}.db.partial"
        BACKUP_GZ="${BACKUP_DIR}/jobs-pre-update-${STAMP}.db.gz"
        # sqlite3 .backup is the Online Backup API — consistent on a live WAL
        # database; no need to stop the service first (same approach as backup.sh).
        sqlite3 "${DB_PATH}" ".backup '${BACKUP_TMP}'"
        gzip -c "${BACKUP_TMP}" > "${BACKUP_GZ}.partial"
        mv "${BACKUP_GZ}.partial" "${BACKUP_GZ}"
        rm -f "${BACKUP_TMP}"
        log "Database backed up to: ${BACKUP_GZ}"
    fi
fi

# ---------------------------------------------------------------------------
# Step 2 — update the source code
# ---------------------------------------------------------------------------
if [[ -n "${WHEEL}" ]]; then
    # Explicit wheel path provided — validate and use it directly.
    [[ -f "${WHEEL}" ]] || die "Wheel file not found: ${WHEEL}"
    log "Installing wheel: ${WHEEL}"
    "${VENV}/bin/pip" install --quiet "${WHEEL}"
    log "Wheel installed."

elif [[ -d "${PREFIX}/.git" ]]; then
    # Git checkout — pull the latest commits.
    log "Git checkout detected. Running git pull --ff-only ..."
    git -C "${PREFIX}" pull --ff-only
    log "Code updated via git."

    log "Reinstalling package into virtualenv..."
    "${VENV}/bin/pip" install --quiet "${PREFIX}"
    log "Package installed."

else
    # Not a git checkout, no wheel — print instructions and exit gracefully.
    # Use unquoted EOF so ${PREFIX} expands in the heredoc.
    cat >&2 <<EOF

[update] The installation at '${PREFIX}' is not a git checkout and no --wheel
[update] was supplied. To update, either:
[update]
[update]   (a) From a source checkout on this machine, rsync it in and reinstall:
[update]
[update]       rsync -a --delete \\
[update]           --exclude .git --exclude .venv --exclude .local \\
[update]           <repo-path>/ ${PREFIX}/
[update]       bash ${PREFIX}/deploy/bambu-bridge-update.sh
[update]
[update]   (b) Supply a pre-built wheel:
[update]
[update]       bash deploy/bambu-bridge-update.sh --wheel /path/to/bambu_bridge-x.y.z-py3-none-any.whl
[update]
[update] See deploy/DEPLOY.md §Updating for the full runbook.
EOF
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3 — restart the service (unless --no-restart)
# ---------------------------------------------------------------------------
if [[ "${NO_RESTART}" -eq 1 ]]; then
    log "--no-restart set — skipping service restart."
    log "Run 'systemctl --user restart bambu-bridge' (or 'sudo systemctl restart bambu-bridge') manually."
else
    # Detect per-user vs system unit.
    if systemctl --user is-active bambu-bridge.service >/dev/null 2>&1 \
       || systemctl --user is-enabled bambu-bridge.service >/dev/null 2>&1; then
        SYSTEMCTL_SCOPE="--user"
    elif systemctl is-active bambu-bridge.service >/dev/null 2>&1 \
         || systemctl is-enabled bambu-bridge.service >/dev/null 2>&1; then
        SYSTEMCTL_SCOPE=""
    else
        warn "bambu-bridge.service not found (neither user-unit nor system-unit)."
        warn "If you installed it manually, restart it yourself and re-run with --no-restart."
        SYSTEMCTL_SCOPE=""
        NO_RESTART=1
    fi

    if [[ "${NO_RESTART}" -eq 0 ]]; then
        log "Restarting bambu-bridge.service (${SYSTEMCTL_SCOPE:-system}) ..."
        if [[ -n "${SYSTEMCTL_SCOPE}" ]]; then
            systemctl --user restart bambu-bridge.service
        else
            # System unit — may need sudo; this is the operator's machine.
            sudo systemctl restart bambu-bridge.service
        fi
        log "Service restarted. Waiting up to ${HEALTH_WAIT_SECS}s for /api/v1/health ..."

        # ---------------------------------------------------------------------------
        # Step 4 — wait for the bridge to come back up
        # ---------------------------------------------------------------------------
        elapsed=0
        while [[ "${elapsed}" -lt "${HEALTH_WAIT_SECS}" ]]; do
            status="$(curl -fsS --max-time 3 "${HEALTH_ENDPOINT}" 2>/dev/null || true)"
            if printf '%s' "${status}" | grep -q '"ok"'; then
                log "Bridge is up. (${elapsed}s)"
                break
            fi
            sleep "${HEALTH_POLL_SECS}"
            elapsed=$(( elapsed + HEALTH_POLL_SECS ))
        done

        if [[ "${elapsed}" -ge "${HEALTH_WAIT_SECS}" ]]; then
            warn "Bridge did not respond on ${HEALTH_ENDPOINT} within ${HEALTH_WAIT_SECS}s."
            warn "Check: journalctl --user -u bambu-bridge -n 50"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Step 5 — report old vs new version
# ---------------------------------------------------------------------------
NEW_VERSION="$(fetch_version)"

echo ""
echo "-----------------------------------------------------------"
if [[ -n "${OLD_VERSION}" && -n "${NEW_VERSION}" ]]; then
    if [[ "${OLD_VERSION}" == "${NEW_VERSION}" ]]; then
        echo "  Bridge version: ${NEW_VERSION}  (unchanged — already up to date)"
    else
        echo "  OLD version: ${OLD_VERSION}"
        echo "  NEW version: ${NEW_VERSION}"
        echo "  Update applied."
    fi
elif [[ -n "${NEW_VERSION}" ]]; then
    echo "  Bridge version: ${NEW_VERSION}"
elif [[ -n "${OLD_VERSION}" ]]; then
    echo "  Old version was: ${OLD_VERSION}"
    echo "  Bridge is not responding yet — check service status."
else
    echo "  Bridge is not responding — check service status."
fi
echo "-----------------------------------------------------------"
echo ""
log "Done."
