#!/usr/bin/env bash
# Produce a CLEAN release tree from the current HEAD.
#
# Usage:  bash scripts/export-release.sh [TARGET_DIR]
#
# Default target: ../bambu-bridge  (a sibling of the dev repo root)
#
# What this script does:
#   1. Runs 'git archive HEAD' — this naturally includes ONLY tracked files,
#      so no untracked dev-notes/, .local/, *.jsonl captures, *.pem secrets,
#      or other dev-only artefacts can sneak in.
#   2. Extracts into TARGET_DIR (created if absent; must be empty or not exist).
#   3. Greps the resulting tree for known sensitive keywords (SILVER, CONNECT)
#      and aborts with a non-zero exit code if any are found.
#
# The coordinator then:
#   git init TARGET_DIR && git -C TARGET_DIR add -A
#   git -C TARGET_DIR commit -m "chore: initial release v0.1.0"
#   git -C TARGET_DIR remote add origin <real-remote-url>
#   git -C TARGET_DIR push -u origin main
#
# IMPORTANT: the DEV repo's git HISTORY contains relocated infra notes and
# must NEVER be pushed to a public remote.  Always export via this script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET="${1:-${REPO_ROOT}/../bambu-bridge}"

echo "=== Bambu Bridge release export ==="
echo "Source:  ${REPO_ROOT}"
echo "Target:  ${TARGET}"
echo ""

# Refuse to clobber a non-empty target that is not our own prior export.
if [[ -e "${TARGET}" ]]; then
    if [[ "$(ls -A "${TARGET}" 2>/dev/null)" ]]; then
        echo "error: target '${TARGET}' already exists and is not empty." >&2
        echo "       Remove it or choose a different path." >&2
        exit 1
    fi
fi

mkdir -p "${TARGET}"

# Export tracked files via git archive.
echo "Running git archive HEAD..."
git -C "${REPO_ROOT}" archive HEAD | tar -x -C "${TARGET}"

echo "Exported $(find "${TARGET}" -type f | wc -l | tr -d ' ') files."
echo ""

# Safety guard — check for secret/internal filenames and specific infra tokens.
echo "Scanning for secret filenames..."
fname_hits="$(find "${TARGET}" \( \
    -iname 'SILVER-ACCESS*' -o -iname 'CONNECTING*' -o -iname 'recommend-*' \
    -o -iname 'SYNTHESIS-*' -o -iname 'REPORT.md' -o -iname 'FOLLOWUP*' \
    -o -path '*/dev-notes/*' -o -iname '*.jsonl' \
    -o \( -iname '*.pem' ! -iname 'p1s-ca.pem.example' \) \
\) 2>/dev/null || true)"

if [[ -n "${fname_hits}" ]]; then
    echo "" >&2
    echo "ABORT: Secret/internal file found in the exported tree:" >&2
    echo "${fname_hits}" >&2
    echo "" >&2
    echo "Review and sanitise before releasing." >&2
    rm -rf "${TARGET}"
    exit 1
fi

echo "Scanning for leaked infra tokens..."
# Exclude the release-tooling files themselves: they legitimately contain the
# token list as their detection pattern (scanning them would self-trigger).
content_hits="$(grep -r --include="*.py" --include="*.md" --include="*.txt" \
        --include="*.toml" --include="*.yaml" --include="*.yml" \
        --include="*.sh" --include="*.json" \
        --exclude="export-release.sh" --exclude="Makefile" \
        -l -E "PRIVATE_DEPLOYMENT_SENTINEL" "${TARGET}" 2>/dev/null || true)"

if [[ -n "${content_hits}" ]]; then
    echo "" >&2
    echo "ABORT: Leaked infra token found in the following exported files:" >&2
    echo "${content_hits}" >&2
    echo "" >&2
    echo "Review and sanitise before releasing." >&2
    rm -rf "${TARGET}"
    exit 1
fi

echo "Clean — no secrets found."
echo ""
echo "=== Export complete ==="
echo ""
echo "Next steps:"
echo "  cd ${TARGET}"
echo "  git init"
echo "  git add -A"
echo "  git commit -m 'chore: initial public release v0.1.0'"
echo "  git remote add origin https://github.com/REPLACE-ME/bambu-bridge.git"
echo "  git push -u origin main"
