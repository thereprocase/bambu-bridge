#!/usr/bin/env bash
# Check the built wheel and sdist in dist/: no secret or internal files, and the
# package data the service needs at runtime is present.
#
# Usage: scripts/verify-dist.sh [DIST_DIR]
set -euo pipefail

DIST="${1:-dist}"
die() { echo "verify-dist: $*" >&2; exit 1; }

sdist="$(find "$DIST" -maxdepth 1 -name 'bambu_bridge-*.tar.gz' | sort -V | tail -1)"
wheel="$(find "$DIST" -maxdepth 1 -name 'bambu_bridge-*.whl' | sort -V | tail -1)"
[ -n "$sdist" ] || die "no sdist in $DIST"
[ -n "$wheel" ] || die "no wheel in $DIST"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar -xzf "$sdist" -C "$work"
python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
    "$wheel" "$work/wheel"

hits="$(find "$work" \( \
    -iname 'SILVER-ACCESS*' -o -iname 'CONNECTING*' -o -iname 'recommend-*' \
    -o -iname 'SYNTHESIS-*' -o -iname 'REPORT.md' -o -iname 'FOLLOWUP*' \
    -o -path '*/dev-notes/*' -o -iname '*.jsonl' -o -iname '.env' -o -iname '*.keystore' \
    -o -iname '*.key' -o \( -iname '*.pem' ! -iname 'p1s-ca.pem.example' \) \
    -o -path '*/node_modules/*' \
\) -print)"
[ -z "$hits" ] || die "secret or internal file in the artefacts:
$hits"
if grep -rlE 'PRIVATE_DEPLOYMENT_SENTINEL' "$work" >/dev/null 2>&1; then
    die "leaked infrastructure marker in the artefacts"
fi
echo "verify-dist: secret scan: no listed names or markers"

package="$work/wheel/bambu_bridge"
for required in db/schema.sql static/viewer.html static/app/index.html hms_catalog.json \
        data/stock_materials_v242.json __init__.py; do
    [ -e "$package/$required" ] || die "$required missing from the wheel"
done
find "$work/wheel" -path '*.dist-info/licenses/*' -name 'LICENSE*' | grep -q . \
    || find "$work/wheel" -path '*.dist-info/*' -name 'LICENSE*' | grep -q . \
    || die "LICENSE missing from the wheel"
echo "verify-dist: required package data present"
echo "verify-dist: PASSED"
