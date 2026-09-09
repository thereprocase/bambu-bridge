#!/usr/bin/env bash
# Package the Bambu Bridge HA integration into a distributable zip.
#
# Usage:  bash homeassistant/integration/release-zip.sh
#
# Produces:  dist/bambu_bridge_integration-<version>.zip
#
# The version is read from the integration's manifest.json.  The zip contains
# a custom_components/bambu_bridge/ directory at its root so it can be
# extracted directly into a Home Assistant config directory.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${here}/../.." && pwd)"
integration_src="${here}/custom_components/bambu_bridge"
manifest="${integration_src}/manifest.json"
dist_dir="${repo_root}/dist"

if [[ ! -f "${manifest}" ]]; then
    echo "error: manifest.json not found at ${manifest}" >&2
    exit 1
fi

# Read version from manifest.json (requires python3 or jq).
if command -v python3 &>/dev/null; then
    version="$(python3 -c "import json,sys; print(json.load(open('${manifest}'))['version'])")"
elif command -v jq &>/dev/null; then
    version="$(jq -r .version "${manifest}")"
else
    echo "error: neither python3 nor jq found — cannot read version from manifest.json" >&2
    exit 1
fi

if [[ -z "${version}" ]]; then
    echo "error: could not read version from ${manifest}" >&2
    exit 1
fi

mkdir -p "${dist_dir}"
out="${dist_dir}/bambu_bridge_integration-${version}.zip"

# Build the zip with custom_components/bambu_bridge/ at the root, using Python's
# zipfile so no external `zip`/`unzip` binary is required (portable across hosts;
# we already depend on Python). Excludes __pycache__ and compiled bytecode.
python3 - "${here}" "${out}" <<'PY'
import os, sys, zipfile
base, out = sys.argv[1], sys.argv[2]
root = os.path.join(base, "custom_components", "bambu_bridge")
entries = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for fn in filenames:
        if fn.endswith((".pyc", ".pyo")):
            continue
        full = os.path.join(dirpath, fn)
        entries.append((full, os.path.relpath(full, base)))
entries.sort(key=lambda t: t[1])
repo_root = os.path.abspath(os.path.join(base, "..", ".."))
for name in ("LICENSE", "NOTICE", "THIRD_PARTY.md"):
    entries.append((os.path.join(repo_root, name), name))
licenses = os.path.join(repo_root, "LICENSES")
for name in sorted(os.listdir(licenses)):
    path = os.path.join(licenses, name)
    if os.path.isfile(path):
        entries.append((path, "LICENSES/" + name))
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for full, arc in entries:
        z.write(full, arc)
print(f"  Contents: {len(entries)} files")
for _, arc in entries:
    print("   ", arc)
PY

echo "Integration zip -> ${out}"
echo "  Version: ${version}"
