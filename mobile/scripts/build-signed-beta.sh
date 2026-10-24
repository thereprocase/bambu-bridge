#!/usr/bin/env bash
# Build the owner-signed ARM64 beta APK and stage it through scripts/stage-beta.py.
#
# Usage: scripts/build-signed-beta.sh OUT_DIR
#
# Uses the existing release key (never creates one). The keystore password is
# read from the Secret Service keyring, as documented beside the keystore, into
# the Gradle process environment only; it is never printed, logged or passed as
# a command-line argument. The build is offline: Gradle resolves only cached
# dependencies.
#
# Env overrides: RELEASE_KEYSTORE, ANDROID_HOME, JAVA_HOME, BUILD_TOOLS.
set -euo pipefail

OUT="${1:?usage: scripts/build-signed-beta.sh OUT_DIR}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEYSTORE="${RELEASE_KEYSTORE:-$HOME/.config/bambu-bridge-app/signing/release-2026.keystore}"
EXPECTED_SIGNER=9506816c97a185058437bca6125ab5ac9d59763f1f128bc55e1844839d502da6
BASELINE_CODE=58
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk}"
BUILD_TOOLS="${BUILD_TOOLS:-$(find "$ANDROID_HOME/build-tools" -mindepth 1 -maxdepth 1 -type d | sort -V | tail -1)}"

die() { echo "build-signed-beta: $*" >&2; exit 1; }
cd "$REPO"
[ -z "$(git status --porcelain)" ] || die "commit or isolate local changes first"
[ -f "$KEYSTORE" ] || die "release keystore not found at $KEYSTORE"
VERSION="$(python3 -c 'import json; print(json.load(open("package.json"))["version"])')"

# The ignored android/app/release.keystore must point at the existing key.
link=android/app/release.keystore
if [ -e "$link" ] || [ -L "$link" ]; then
    [ "$(readlink -f "$link")" = "$(readlink -f "$KEYSTORE")" ] \
        || die "$link exists and is not the existing release key"
else
    ln -s "$KEYSTORE" "$link"
fi

command -v secret-tool >/dev/null || die "secret-tool is required to read the keyring entry"
RELEASE_STORE_PASSWORD="$(secret-tool lookup application bambu-bridge-app purpose release-2026)" \
    || die "keyring entry unavailable (unlock the keyring and retry)"
[ -n "$RELEASE_STORE_PASSWORD" ] || die "keyring entry is empty"
RELEASE_KEY_PASSWORD="$RELEASE_STORE_PASSWORD"
export RELEASE_STORE_PASSWORD RELEASE_KEY_PASSWORD

echo "Building $VERSION (arm64-v8a) at $(git rev-parse --short HEAD)"
(cd android && NODE_ENV=production ./gradlew --offline --no-daemon --max-workers=3 \
    -PreactNativeArchitectures=arm64-v8a assembleRelease)
unset RELEASE_STORE_PASSWORD RELEASE_KEY_PASSWORD

python3 scripts/stage-beta.py \
    --apk android/app/build/outputs/apk/release/app-release.apk \
    --build-tools "$BUILD_TOOLS" \
    --expected-signer-sha256 "$EXPECTED_SIGNER" \
    --version "$VERSION" \
    --baseline-version-code "$BASELINE_CODE" \
    --out "$OUT"
