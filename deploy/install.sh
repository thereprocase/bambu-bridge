#!/usr/bin/env bash
# deploy/install.sh — one-command installer for Bambu Bridge.
#
# Usage:
#   bash deploy/install.sh [OPTIONS]
#
# Options:
#   -y, --non-interactive   Skip all prompts; accept defaults.
#   --prefix DIR            Install venv + code under DIR  (default: ~/bambu-bridge).
#   --no-service            Skip systemd unit installation entirely.
#   -h, --help              Show this help and exit.
#
# What it does:
#   1. Verifies python3.12 is present.
#   2. Creates PREFIX/.venv and installs the package (prefers dist/*.whl,
#      falls back to 'pip install .' from the checkout root).
#   3. Creates ~/.config/bambu-bridge/bridge.env (from the example template if
#      not present) and generates BRIDGE_API_KEY if it is not already set.
#      The file is chmod 600 so only this user can read it.
#   4. Installs + enables the systemd user unit (skip with --no-service).
#   5. Prints useful post-install hints.
#
# Idempotent: safe to re-run. Never overwrites an existing BRIDGE_API_KEY.
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PREFIX="${HOME}/bambu-bridge"
NON_INTERACTIVE=false
NO_SERVICE=false
CURRENT_USER="${USER:-}"

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<'EOF'
Usage: bash deploy/install.sh [OPTIONS]

Options:
  -y, --non-interactive   Accept all defaults without prompting.
  --prefix DIR            Install venv and code under DIR (default: ~/bambu-bridge).
  --no-service            Skip systemd unit installation.
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
        --no-service)
            NO_SERVICE=true
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
    # confirm "Question?" — returns 0 (yes) or 1 (no); auto-yes in non-interactive mode.
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

# ---------------------------------------------------------------------------
# Sanity: $USER must be set
# ---------------------------------------------------------------------------
if [[ -z "${CURRENT_USER}" ]]; then
    fail "\$USER is unset — cannot determine the running user."
fi

# ---------------------------------------------------------------------------
# Step 1: Verify python3.12
# ---------------------------------------------------------------------------
info "Checking prerequisites..."

if ! command -v python3.12 >/dev/null 2>&1; then
    cat >&2 <<EOF

error: python3.12 not found on PATH.

Bambu Bridge requires Python 3.12 or later. To install it:

  Debian/Ubuntu:   sudo apt install python3.12 python3.12-venv
  Fedora/RHEL:     sudo dnf install python3.12
  Arch:            sudo pacman -S python
  macOS (Homebrew):brew install python@3.12

After installation, re-run this script.
EOF
    exit 1
fi

PYTHON="$(command -v python3.12)"
PY_VERSION="$("${PYTHON}" --version 2>&1)"
log "Found: ${PY_VERSION} at ${PYTHON}"

if ! command -v openssl >/dev/null 2>&1; then
    fail "openssl not found — required to generate BRIDGE_API_KEY. Install it and re-run."
fi

# ---------------------------------------------------------------------------
# Step 2: Create venv and install the package
# ---------------------------------------------------------------------------
info "Installing into ${PREFIX}..."

VENV="${PREFIX}/.venv"

mkdir -p "${PREFIX}"

if [[ ! -d "${VENV}" ]]; then
    log "Creating virtualenv at ${VENV}"
    "${PYTHON}" -m venv "${VENV}"
else
    log "Virtualenv already exists at ${VENV}"
fi

log "Upgrading pip..."
"${VENV}/bin/pip" install --quiet --upgrade pip

# Prefer a pre-built wheel in dist/ (faster, no build deps on the host).
WHEEL=""
if [[ -d "${REPO_ROOT}/dist" ]]; then
    # Use ls -t to find the newest wheel; avoid command substitution in pipelines.
    WHEEL="$(ls -1t "${REPO_ROOT}/dist"/*.whl 2>/dev/null | head -n 1 || true)"
fi

if [[ -n "${WHEEL}" ]]; then
    log "Installing from wheel: ${WHEEL}"
    "${VENV}/bin/pip" install --quiet "${WHEEL}"
else
    log "No pre-built wheel found in dist/ — installing from source checkout..."
    "${VENV}/bin/pip" install --quiet "${REPO_ROOT}"
fi

log "Package installed."

# ---------------------------------------------------------------------------
# Step 3: Write ~/.config/bambu-bridge/bridge.env
# ---------------------------------------------------------------------------
info "Configuring bridge environment..."

CONFIG_DIR="${HOME}/.config/bambu-bridge"
ENV_FILE="${CONFIG_DIR}/bridge.env"
ENV_EXAMPLE="${SCRIPT_DIR}/bridge.env.example"

mkdir -p "${CONFIG_DIR}"

# Create the env file from the example template if it does not yet exist.
if [[ ! -f "${ENV_FILE}" ]]; then
    if [[ ! -f "${ENV_EXAMPLE}" ]]; then
        fail "bridge.env.example not found at ${ENV_EXAMPLE}"
    fi
    cp "${ENV_EXAMPLE}" "${ENV_FILE}"
    log "Created ${ENV_FILE} from template."
fi

# Always enforce 600 — even if the file already existed with looser perms.
chmod 600 "${ENV_FILE}"

# Generate BRIDGE_API_KEY if it is not already set (blank or missing line).
EXISTING_KEY=""
if grep -q '^BRIDGE_API_KEY=.\+' "${ENV_FILE}" 2>/dev/null; then
    EXISTING_KEY="$(grep '^BRIDGE_API_KEY=' "${ENV_FILE}" | head -n 1 | cut -d= -f2-)"
fi

if [[ -z "${EXISTING_KEY}" ]]; then
    NEW_KEY="$(openssl rand -hex 32)"
    # Replace the BRIDGE_API_KEY= line (with or without a value) in-place.
    # Use a temp file + mv for atomicity — avoids sed -i portability issues.
    TMPENV="${ENV_FILE}.new"
    if grep -q '^BRIDGE_API_KEY=' "${ENV_FILE}"; then
        # Replace the existing (blank) line.
        while IFS= read -r line; do
            if [[ "${line}" == BRIDGE_API_KEY=* ]]; then
                printf 'BRIDGE_API_KEY=%s\n' "${NEW_KEY}"
            else
                printf '%s\n' "${line}"
            fi
        done < "${ENV_FILE}" > "${TMPENV}"
    else
        # Key line is absent entirely — append it.
        cp "${ENV_FILE}" "${TMPENV}"
        printf '\nBRIDGE_API_KEY=%s\n' "${NEW_KEY}" >> "${TMPENV}"
    fi
    mv "${TMPENV}" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    log "Generated new BRIDGE_API_KEY and wrote to ${ENV_FILE}"
else
    log "BRIDGE_API_KEY already set — not overwriting."
fi

# Ensure data directories exist.
DATA_DIR="${HOME}/.local/share/bambu-bridge"
mkdir -p "${DATA_DIR}/backups"
log "Data directory: ${DATA_DIR}"

# ---------------------------------------------------------------------------
# Step 4: Install systemd unit
# ---------------------------------------------------------------------------
if [[ "${NO_SERVICE}" == true ]]; then
    info "Skipping systemd unit (--no-service)."
else
    info "Installing systemd unit..."

    TEMPLATE="${SCRIPT_DIR}/bambu-bridge.service"
    if [[ ! -f "${TEMPLATE}" ]]; then
        fail "Service template not found at ${TEMPLATE}"
    fi

    # Decide between user unit and system unit.
    # Prefer the per-user path (no sudo required, works on most desktops).
    # Fall back to system unit only if the user explicitly confirms + has sudo.
    USE_USER_UNIT=true
    UNIT_DIR="${HOME}/.config/systemd/user"
    UNIT_FILE="${UNIT_DIR}/bambu-bridge.service"

    # Detect whether systemd --user is available.
    if ! systemctl --user status >/dev/null 2>&1 && \
       ! systemctl --user list-units >/dev/null 2>&1; then
        warn "systemd --user does not appear to be available."
        if command -v sudo >/dev/null 2>&1 && \
           confirm "Install as a system unit (/etc/systemd/system/) instead?"; then
            USE_USER_UNIT=false
            UNIT_DIR="/etc/systemd/system"
            UNIT_FILE="${UNIT_DIR}/bambu-bridge.service"
        else
            warn "Skipping systemd unit installation."
            NO_SERVICE=true
        fi
    fi

    if [[ "${NO_SERVICE}" != true ]]; then
        mkdir -p "${UNIT_DIR}"

        # Build the concrete unit — substitute BRIDGE_USER_PLACEHOLDER and also
        # rewrite WorkingDirectory / ExecStart to point at PREFIX if it differs
        # from the default ~/bambu-bridge.
        TMPUNIT="${UNIT_FILE}.new"
        while IFS= read -r line; do
            # Substitute the user placeholder.
            line="${line//BRIDGE_USER_PLACEHOLDER/${CURRENT_USER}}"
            # If a non-default prefix was requested, rewrite the two path lines.
            if [[ "${PREFIX}" != "${HOME}/bambu-bridge" ]]; then
                line="${line//%h\/bambu-bridge/${PREFIX}}"
            fi
            printf '%s\n' "${line}"
        done < "${TEMPLATE}" > "${TMPUNIT}"

        if [[ "${USE_USER_UNIT}" == true ]]; then
            mv "${TMPUNIT}" "${UNIT_FILE}"
            log "Written -> ${UNIT_FILE}"
            systemctl --user daemon-reload
            systemctl --user enable bambu-bridge.service
            systemctl --user start  bambu-bridge.service || \
                warn "Service start returned non-zero — check 'systemctl --user status bambu-bridge'."
            SYSTEMCTL_CMD="systemctl --user"
            JOURNAL_CMD="journalctl --user -u bambu-bridge -f"
        else
            sudo mv "${TMPUNIT}" "${UNIT_FILE}"
            log "Written -> ${UNIT_FILE}"
            sudo systemctl daemon-reload
            sudo systemctl enable --now bambu-bridge.service || \
                warn "Service start returned non-zero — check 'sudo systemctl status bambu-bridge'."
            SYSTEMCTL_CMD="sudo systemctl"
            JOURNAL_CMD="journalctl -u bambu-bridge -f"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Step 5: Post-install hints
# ---------------------------------------------------------------------------
BRIDGE_PORT=8080
if [[ -f "${ENV_FILE}" ]]; then
    ENV_PORT="$(grep '^BRIDGE_PORT=' "${ENV_FILE}" 2>/dev/null | head -n 1 | cut -d= -f2-)"
    if [[ -n "${ENV_PORT}" ]]; then
        BRIDGE_PORT="${ENV_PORT}"
    fi
fi

HOSTNAME_LOCAL="$(hostname 2>/dev/null || echo "localhost")"

echo ""
echo "================================================================"
echo " Bambu Bridge installed successfully."
echo "================================================================"
echo ""
echo " Config:  ${ENV_FILE}"
echo " Venv:    ${VENV}"
echo " Data:    ${DATA_DIR}"
echo ""

if [[ "${NO_SERVICE}" != true ]]; then
    echo " Service status:"
    echo "   ${SYSTEMCTL_CMD:-systemctl --user} status bambu-bridge"
    echo ""
    echo " Live logs:"
    echo "   ${JOURNAL_CMD:-journalctl --user -u bambu-bridge -f}"
    echo ""
    echo " To survive reboots without an active login session:"
    echo "   sudo loginctl enable-linger ${CURRENT_USER}"
    echo ""
fi

echo " Health check (once the service is running):"
echo "   curl -fsS http://localhost:${BRIDGE_PORT}/api/v1/health"
echo ""
echo " Web app:  http://${HOSTNAME_LOCAL}:${BRIDGE_PORT}/app"
echo " Viewer:   http://${HOSTNAME_LOCAL}:${BRIDGE_PORT}/viz"
echo ""
echo " Over Tailscale — from any device on your tailnet:"
echo "   http://${HOSTNAME_LOCAL}:${BRIDGE_PORT}/app"
echo "   (use your Tailscale machine name if the hostname differs)"
echo ""
echo " Next: add your printer with:"
echo "   curl -fsS -X POST http://localhost:${BRIDGE_PORT}/api/v1/printers \\"
echo "     -H 'Authorization: Bearer <your BRIDGE_API_KEY>' \\"
echo "     -H 'Content-Type: application/json' \\"
echo "     -d '{\"host\":\"<printer-ip>\",\"access_code\":\"<8-digit code>\",\"friendly_name\":\"P1S\"}'"
echo ""
echo " See deploy/DEPLOY.md for the full runbook."
echo ""
