#!/usr/bin/env bash
# Copy the Bambu Bridge integration into a Home Assistant config directory.
#
# Usage:  install.sh /path/to/homeassistant/config
#
# The target is your HA config dir (the one with configuration.yaml). The
# integration lands at <target>/custom_components/bambu_bridge/. Restart Home
# Assistant afterwards, then add the integration from the UI.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 /path/to/homeassistant/config" >&2
    exit 1
fi

target="$1"
if [[ ! -d "${target}" ]]; then
    echo "error: ${target} is not a directory" >&2
    exit 1
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="${here}/custom_components/bambu_bridge"
dest="${target}/custom_components/bambu_bridge"

mkdir -p "${target}/custom_components"
rm -rf "${dest}"
cp -r "${src}" "${dest}"

echo "Installed -> ${dest}"
echo "Restart Home Assistant, then: Settings → Devices & Services → Add Integration → Bambu Bridge."
