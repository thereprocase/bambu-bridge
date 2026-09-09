#!/usr/bin/with-contenv bashio
# Bambu Bridge add-on entrypoint.
#
# Maps the add-on options onto the bridge's env-driven Settings
# (src/bambu_bridge/config.py) and execs the server.
set -e

export BRIDGE_HOST="0.0.0.0"
export BRIDGE_PORT="8080"
export BRIDGE_LOG_FORMAT="json"
export BRIDGE_LOG_LEVEL="$(bashio::config 'log_level')"
export BRIDGE_API_KEY="$(bashio::config 'api_key')"
export BRIDGE_SPAGHETTI_DETECTION="$(bashio::config 'spaghetti_detection')"

# /data is the add-on's persistent volume. The SQLite DB and the TOFU cert
# fingerprints MUST live here to survive add-on updates and restarts.
export BRIDGE_DB_PATH="/data/jobs.db"
export BRIDGE_FILES_DIR="/data/files"
mkdir -p "${BRIDGE_FILES_DIR}"

if bashio::config.has_value 'ntfy_url'; then
    export NTFY_URL="$(bashio::config 'ntfy_url')"
fi
if bashio::config.has_value 'ntfy_topic'; then
    export NTFY_TOPIC="$(bashio::config 'ntfy_topic')"
fi

if ! bashio::config.has_value 'api_key'; then
    bashio::log.warning \
        "No api_key set — the bridge fails closed: every authed route returns 503."
fi

bashio::log.info "Starting Bambu Bridge on 0.0.0.0:8080"
exec bambu-bridge
