#!/usr/bin/env bash
# Point the bambu-bridge unit at a release directory through one managed drop-in.
#
# Usage:
#   release-activate.sh RELEASE_DIR [--env NAME=VALUE]... [--no-restart]
#   release-activate.sh --rollback [--no-restart]
#
# Writes zz-release.conf in the unit's drop-in directory. "zz-" sorts after
# every "99z…" file, so this one file decides WorkingDirectory, ExecStart and
# PYTHONPATH without adding another drop-in per deploy. --env lines belong to
# the release and leave with it on rollback. The previous zz-release.conf is
# kept as zz-release.conf.prev (systemd ignores it); --rollback swaps it back,
# or removes zz-release.conf when there is no previous one, which returns the
# unit to whatever the older drop-ins select.
#
# Before touching the unit the release's gateway and inventory modules must
# import with its own interpreter (main builds settings at import, so it is
# left to the restart) and report a version. After a restart the script waits for /api/v1/health.
#
# Env overrides (tests, other layouts): UNIT (bambu-bridge), DROPIN_DIR,
# RELEASES_ROOT (/opt/bambu-bridge-releases), HEALTH_URL, SYSTEMCTL, HEALTH_WAIT_S.
set -euo pipefail
umask 022

UNIT="${UNIT:-bambu-bridge}"
DROPIN_DIR="${DROPIN_DIR:-/etc/systemd/system/$UNIT.service.d}"
RELEASES_ROOT="${RELEASES_ROOT:-/opt/bambu-bridge-releases}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8080/api/v1/health}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
HEALTH_WAIT_S="${HEALTH_WAIT_S:-60}"
MANAGED="$DROPIN_DIR/zz-release.conf"

die() { echo "release-activate: $*" >&2; exit 1; }
log() { echo "release-activate: $*"; }

release="" rollback=0 restart=1
envs=()
while [ $# -gt 0 ]; do
    case "$1" in
        --rollback) rollback=1; shift ;;
        --no-restart) restart=0; shift ;;
        --env)
            [ $# -ge 2 ] || die "--env needs NAME=VALUE"
            [[ "$2" =~ ^[A-Z][A-Z0-9_]*=[^[:space:]\"\\]*$ ]] || die "invalid --env $2"
            envs+=("$2"); shift 2 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option $1" ;;
        *) [ -z "$release" ] || die "one release only"; release="$1"; shift ;;
    esac
done

restart_and_wait() {
    "$SYSTEMCTL" daemon-reload
    [ "$restart" -eq 1 ] || { log "unit reloaded; not restarted (--no-restart)"; return 0; }
    "$SYSTEMCTL" restart "$UNIT"
    for _ in $(seq "$HEALTH_WAIT_S"); do
        if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
            log "healthy at $HEALTH_URL"
            return 0
        fi
        sleep 1
    done
    die "no healthy response from $HEALTH_URL within ${HEALTH_WAIT_S}s; consider --rollback"
}

mkdir -p "$DROPIN_DIR"

if [ "$rollback" -eq 1 ]; then
    [ -z "$release" ] && [ "${#envs[@]}" -eq 0 ] || die "--rollback takes no release or --env"
    if [ -f "$MANAGED.prev" ]; then
        mv -f "$MANAGED.prev" "$MANAGED"
        log "restored the previous zz-release.conf"
    elif [ -f "$MANAGED" ]; then
        mv -f "$MANAGED" "$MANAGED.rolled-back"
        log "removed zz-release.conf; the older drop-ins select the release again"
    else
        die "nothing to roll back"
    fi
    restart_and_wait
    exit 0
fi

[ -n "$release" ] || die "usage: release-activate.sh RELEASE_DIR [--env NAME=VALUE]..."
release="$(realpath -e "$release")" || die "release directory does not exist"
case "$release" in
    "$RELEASES_ROOT"/*) ;;
    *) die "$release is not under $RELEASES_ROOT" ;;
esac
[[ "$release" =~ ^[A-Za-z0-9/._-]+$ ]] || die "release path has characters unsafe for a unit file"
python="$release/.venv/bin/python"
[ -x "$python" ] || die "no interpreter at $python"
[ -f "$release/src/bambu_bridge/__init__.py" ] || die "no src/bambu_bridge in $release"

version="$(cd / && PYTHONPATH="$release/src" "$python" -I -c \
    'import sys; sys.path.insert(0, sys.argv[1]); import uvicorn, bambu_bridge, bambu_bridge.native_gateway, bambu_bridge.api.inventory; print(bambu_bridge.__version__)' \
    "$release/src")" || die "the release does not import with its interpreter"
log "release $release imports as version $version"

temporary="$(mktemp "$DROPIN_DIR/.zz-release.XXXXXX")"
trap 'rm -f "$temporary"' EXIT
{
    echo "# Managed by deploy/release-activate.sh; replaced on every activation."
    echo "# release-version=$version"
    echo "[Service]"
    echo "WorkingDirectory=$release"
    echo "Environment=PYTHONPATH=$release/src"
    for item in "${envs[@]}"; do
        echo "Environment=$item"
    done
    echo "ExecStart="
    echo "ExecStart=$python -m bambu_bridge.main"
} > "$temporary"
chmod 0644 "$temporary"
if [ -f "$MANAGED" ]; then
    cp -p "$MANAGED" "$MANAGED.prev"
fi
mv -f "$temporary" "$MANAGED"
trap - EXIT
log "wrote $MANAGED"
restart_and_wait
