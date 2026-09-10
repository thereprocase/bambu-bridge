#!/usr/bin/env bash
# deploy/uninstall.sh — remove Bambu Bridge from this host.
#
# Usage:
#   bash deploy/uninstall.sh [OPTIONS]
#
# Options:
#   -y, --non-interactive   Accept all defaults without prompting.
#   --prefix DIR            Venv / code prefix to remove (default: ~/bambu-bridge).
#   --purge                 Also remove config (~/.config/bambu-bridge/) and
#                           data (~/.local/share/bambu-bridge/).  A confirmation
#                           prompt is shown unless -y is also given.
#   -h, --help              Show this help and exit.
#
# Default behaviour (no --purge):
#   - Stop + disable the systemd unit (user unit first, system unit if found).
#   - Remove the unit file.
#   - Remove the venv and installed prefix (~/bambu-bridge by default).
#   - PRESERVE ~/.config/bambu-bridge/ (API key, settings).
#   - PRESERVE ~/.local/share/bambu-bridge/ (job database, uploaded files, backups).
#
# With --purge: also removes config and data after confirmation.
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PREFIX="${HOME}/bambu-bridge"
NON_INTERACTIVE=false
PURGE=false
CURRENT_USER="${USER:-}"

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<'EOF'
Usage: bash deploy/uninstall.sh [OPTIONS]

Options:
  -y, --non-interactive   Accept all defaults without prompting.
  --prefix DIR            Prefix that was used at install time (default: ~/bambu-bridge).
  --purge                 Also delete config and data (irreversible).
  -h, --help              Show this help and exit.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--non-interactive)
            NON_INTERACTIVE=true
            shift
            ;;
        --prefix)
            if [[ -z "${2:-}" ]]; then
                echo "error: --prefix requires an argument." >&2
                exit 1
            fi
            PREFIX="$2"
            shift 2
            ;;
        --purge)
            PURGE=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "  $*"; }
info() { echo ""; echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }
fail() { echo "error: $*" >&2; exit 1; }

confirm() {
    local prompt="$1"
    if [[ "${NON_INTERACTIVE}" == true ]]; then
        return 0
    fi
    local answer
    read -r -p "${prompt} [y/N] " answer
    case "${answer}" in
        [Yy]|[Yy][Ee][Ss]) return 0 ;;
        *)                  return 1 ;;
    esac
}

if [[ -z "${CURRENT_USER}" ]]; then
    fail "\$USER is unset — cannot determine the running user."
fi

# ---------------------------------------------------------------------------
# Step 1: Stop and disable the systemd unit
# ---------------------------------------------------------------------------
info "Stopping and disabling bambu-bridge service..."

USER_UNIT="${HOME}/.config/systemd/user/bambu-bridge.service"
SYSTEM_UNIT="/etc/systemd/system/bambu-bridge.service"

REMOVED_UNIT=false

# Per-user unit (no sudo needed).
if [[ -f "${USER_UNIT}" ]]; then
    log "Found user unit: ${USER_UNIT}"
    if systemctl --user is-active bambu-bridge.service >/dev/null 2>&1; then
        log "Stopping service..."
        systemctl --user stop bambu-bridge.service || warn "Stop returned non-zero — continuing."
    fi
    if systemctl --user is-enabled bambu-bridge.service >/dev/null 2>&1; then
        log "Disabling service..."
        systemctl --user disable bambu-bridge.service || warn "Disable returned non-zero — continuing."
    fi
    rm -f "${USER_UNIT}"
    log "Removed ${USER_UNIT}"
    systemctl --user daemon-reload 2>/dev/null || true
    REMOVED_UNIT=true
fi

# System unit (needs sudo).
if [[ -f "${SYSTEM_UNIT}" ]]; then
    log "Found system unit: ${SYSTEM_UNIT}"
    if command -v sudo >/dev/null 2>&1; then
        if sudo systemctl is-active bambu-bridge.service >/dev/null 2>&1; then
            log "Stopping system service..."
            sudo systemctl stop bambu-bridge.service || warn "Stop returned non-zero — continuing."
        fi
        if sudo systemctl is-enabled bambu-bridge.service >/dev/null 2>&1; then
            log "Disabling system service..."
            sudo systemctl disable bambu-bridge.service || warn "Disable returned non-zero — continuing."
        fi
        sudo rm -f "${SYSTEM_UNIT}"
        log "Removed ${SYSTEM_UNIT}"
        sudo systemctl daemon-reload 2>/dev/null || true
        REMOVED_UNIT=true
    else
        warn "sudo not available — cannot remove system unit ${SYSTEM_UNIT}. Remove it manually."
    fi
fi

if [[ "${REMOVED_UNIT}" == false ]]; then
    log "No installed systemd unit found — nothing to stop."
fi

# ---------------------------------------------------------------------------
# Step 2: Remove the venv / prefix directory
# ---------------------------------------------------------------------------
info "Removing installation prefix..."

VENV="${PREFIX}/.venv"

if [[ -d "${PREFIX}" ]]; then
    log "Removing ${PREFIX}..."
    rm -rf "${PREFIX}"
    log "Done."
else
    log "${PREFIX} does not exist — nothing to remove."
fi

# ---------------------------------------------------------------------------
# Step 3: (Optional) Purge config and data
# ---------------------------------------------------------------------------
CONFIG_DIR="${HOME}/.config/bambu-bridge"
DATA_DIR="${HOME}/.local/share/bambu-bridge"

if [[ "${PURGE}" == true ]]; then
    echo ""
    echo "WARNING: --purge will permanently delete:"
    echo "  ${CONFIG_DIR}   (contains your BRIDGE_API_KEY and settings)"
    echo "  ${DATA_DIR}   (contains the job database, uploaded files, and backups)"
    echo ""

    if confirm "Permanently delete config and data? This cannot be undone."; then
        info "Purging config and data..."
        if [[ -d "${CONFIG_DIR}" ]]; then
            rm -rf "${CONFIG_DIR}"
            log "Removed ${CONFIG_DIR}"
        else
            log "${CONFIG_DIR} not found — skipping."
        fi
        if [[ -d "${DATA_DIR}" ]]; then
            rm -rf "${DATA_DIR}"
            log "Removed ${DATA_DIR}"
        else
            log "${DATA_DIR} not found — skipping."
        fi
    else
        log "Skipping purge (user declined)."
    fi
else
    echo ""
    echo " Config and data are PRESERVED (use --purge to remove them):"
    echo "   Config: ${CONFIG_DIR}"
    echo "   Data:   ${DATA_DIR}"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "================================================================"
echo " Bambu Bridge uninstalled."
echo "================================================================"
echo ""
if [[ "${PURGE}" != true ]]; then
    echo " Your config and data remain at:"
    echo "   ${CONFIG_DIR}"
    echo "   ${DATA_DIR}"
    echo ""
    echo " To reinstall later, run:  bash deploy/install.sh"
    echo " To remove config+data:    bash deploy/uninstall.sh --purge"
fi
echo ""
