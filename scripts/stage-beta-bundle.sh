#!/usr/bin/env bash
# Stage a bridge beta bundle from a clean HEAD: wheel, sdist, deploy scripts,
# release notes, SHA256SUMS and a manifest naming the exact source commit.
#
# Usage: scripts/stage-beta-bundle.sh OUT_DIR
#
# Builds offline (cached build backend only) and runs scripts/verify-dist.sh. The
# output directory must not exist; nothing is published or installed.
set -euo pipefail

OUT="${1:?usage: scripts/stage-beta-bundle.sh OUT_DIR}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
die() { echo "stage-beta-bundle: $*" >&2; exit 1; }

cd "$REPO"
[ -z "$(git status --porcelain)" ] || die "commit or isolate local changes first"
[ ! -e "$OUT" ] || die "$OUT already exists"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/bambu_bridge/__init__.py)"
grep -q "^version = \"$VERSION\"$" pyproject.toml || die "pyproject.toml version differs from $VERSION"
HEAD="$(git rev-parse HEAD)"

rm -rf dist
uv build --offline
bash scripts/verify-dist.sh dist

mkdir -p "$OUT/deploy"
touch "$OUT/.incomplete"
cp "dist/bambu_bridge-$VERSION-py3-none-any.whl" "dist/bambu_bridge-$VERSION.tar.gz" "$OUT/"
cp deploy/state-backup.sh deploy/state-restore.sh deploy/bambu-bridge-update.sh \
    deploy/release-activate.sh deploy/release-prune.py "$OUT/deploy/"
cp docs/SPOOL-INVENTORY-BETA-RELEASE.md CHANGELOG.md "$OUT/"

cat > "$OUT/manifest.json" <<EOF
{
  "package": "bambu-bridge",
  "version": "$VERSION",
  "source_commit": "$HEAD",
  "built_offline_with": "$(uv --version)",
  "verify_dist": "passed",
  "verification_scope": "build, packaged-file and secret-name checks only",
  "feature_acceptance": "see SPOOL-INVENTORY-BETA-RELEASE.md"
}
EOF
sums="$(mktemp)"
(cd "$OUT" && find . -type f ! -name .incomplete -printf '%P\n' | sort | xargs -d '\n' sha256sum) > "$sums"
mv "$sums" "$OUT/SHA256SUMS"
chmod 644 "$OUT/SHA256SUMS"
rm "$OUT/.incomplete"
echo "staged $VERSION from $HEAD in $OUT"
