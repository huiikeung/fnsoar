#!/bin/bash
# ---------------------------------------------------------------------------
# fnSoar admin server launcher (persisted copy under $TRIM_APPDEST/admin/)
#
# fnOS registers the SERVICE_COMMAND at install time from cmd/service-setup;
# the cmd/ directory does NOT persist into $TRIM_APPDEST, so the admin line
# must reference a script that ships inside app.tgz (this one lands at
# $TRIM_APPDEST/admin/start_admin.sh).
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(dirname "$SCRIPT_DIR")"
DEST_DIR="${TRIM_APPDEST:-${APP_ROOT}}"
DATA_DIR="${TRIM_PKGVAR:-/vol1/@appdata/fnnas.fnsoar}"
ADMIN_PORT="${MIHOMO_ADMIN_PORT:-9099}"

export MIHOMO_APP_NAME="fnnas.fnsoar"
export MIHOMO_DATA_DIR="${DATA_DIR}"
export MIHOMO_DEST_DIR="${DEST_DIR}"
export MIHOMO_ADMIN_PORT="${ADMIN_PORT}"
export MIHOMO_GATEWAY_SOCK="${DEST_DIR}/fnsoar.sock"

# Skip if an admin server is already listening (idempotent restarts)
if (exec 3<>/dev/tcp/127.0.0.1/${ADMIN_PORT}) 2>/dev/null; then
    exit 0
fi

exec python3 "${SCRIPT_DIR}/admin_server.py"
