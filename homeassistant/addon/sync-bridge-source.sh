#!/usr/bin/env bash
# Vendor the bambu-bridge server source into the add-on build context.
#
# Home Assistant builds an add-on with the add-on folder as the Docker build
# context, so the complete reviewed bridge source must be
# copied in before the Supervisor builds the add-on.  Re-run this (or use
# 'make addon-vendor' from the repo root) after any change to src/ or
# pyproject.toml.  Release snapshots commit the generated bridge/ tree so Supervisor can build
# directly from GitHub; this script remains the source of that generated copy.
#
# Usage:
#   bash homeassistant/addon/sync-bridge-source.sh          # normal sync
#   bash homeassistant/addon/sync-bridge-source.sh --check  # drift check only
#
# Exit codes:
#   0  — sync completed (or no drift found in --check mode)
#   1  — drift detected in source, assets, metadata or license notices
set -euo pipefail

CHECK_ONLY=false
if [[ "${1:-}" == "--check" ]]; then
    CHECK_ONLY=true
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${here}/../.." && pwd)"
dest="${here}/bambu-bridge/bridge"
[[ "${dest}" == "${repo_root}/homeassistant/addon/bambu-bridge/bridge" && ! -L "${dest}" ]] || { echo "Unexpected vendor destination" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Drift check — compare every src/ file against its vendored counterpart.
# ---------------------------------------------------------------------------
drift_check() {
    local stale=0
    diff -qr --exclude=__pycache__ --exclude='*.pyc' "${repo_root}/src" "${dest}/src" || stale=1
    diff -qr "${repo_root}/LICENSES" "${dest}/LICENSES" || stale=1
    for relative in pyproject.toml README.md LICENSE NOTICE THIRD_PARTY.md VALIDATION.md; do
        if ! cmp -s "${repo_root}/${relative}" "${dest}/${relative}"; then
            echo "STALE or missing vendored file: ${relative}" >&2
            stale=1
        fi
    done
    return "${stale}"
}

if "${CHECK_ONLY}"; then
    echo "Checking for vendored bridge drift..."
    if drift_check; then
        echo "No drift — vendored copy is up to date."
        exit 0
    else
        echo ""
        echo "ERROR: Vendored bridge is stale. Run 'make addon-vendor' to sync." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Full sync.
# ---------------------------------------------------------------------------
rm -rf "${dest}"
mkdir -p "${dest}"

# Copy ALL of src/bambu_bridge (including newly added modules).
tar -C "${repo_root}" --exclude=__pycache__ --exclude='*.pyc' -cf - src | tar -C "${dest}" -xf -

# Produce a vendored pyproject.toml that carries the ROOT project's runtime
# dependencies verbatim so they can never drift (e.g. cryptography going
# missing as happened previously).  We extract the [project.dependencies]
# block from the root pyproject and splice it into a minimal build file.
#
# Strategy: copy the entire root pyproject.toml.  The add-on Dockerfile runs
# 'pip install /opt/bridge' which reads it directly — the [tool.*] sections
# are harmless and hatchling ignores them at install time.
cp "${repo_root}/pyproject.toml" "${dest}/pyproject.toml"
cp "${repo_root}/README.md"      "${dest}/README.md"
cp "${repo_root}/LICENSE" "${repo_root}/NOTICE" "${repo_root}/THIRD_PARTY.md" "${repo_root}/VALIDATION.md" "${dest}/"
cp -r "${repo_root}/LICENSES" "${dest}/LICENSES"

# Drift checks compare content; copied-file timestamps are not a correctness signal.

py_count="$(find "${dest}/src" -name '*.py' | wc -l | tr -d ' ')"
echo "Vendored bridge source -> ${dest}"
echo "  ${py_count} python files"
echo "  pyproject.toml (synced from root — deps include cryptography and all runtime deps)"
echo "  README.md"
echo ""
echo "Now rebuild the add-on:"
echo "  Home Assistant -> Settings -> Add-ons -> Bambu Bridge -> Rebuild."
