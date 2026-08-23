#!/bin/bash
# ---------------------------------------------------------------------------
# Clash Mini admin server launcher (legacy copy under cmd/)
# NOTE: cmd/ does NOT persist into $TRIM_APPDEST after install. The copy
# that actually runs is $TRIM_APPDEST/admin/start_admin.sh (ships inside
# app.tgz). This file is kept for reference / direct invocation only.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(dirname "$SCRIPT_DIR")"
DEST_DIR="${TRIM_APPDEST:-${APP_ROOT}}"
DATA_DIR="${TRIM_PKGVAR:-/vol1/@appdata/fnnas.clashmini}"
ADMIN_PORT="${MIHOMO_ADMIN_PORT:-9099}"

export MIHOMO_APP_NAME="fnnas.clashmini"
export MIHOMO_DATA_DIR="${DATA_DIR}"
export MIHOMO_DEST_DIR="${DEST_DIR}"
export MIHOMO_ADMIN_PORT="${ADMIN_PORT}"
export MIHOMO_GATEWAY_SOCK="${DEST_DIR}/clashmini.sock"

if (exec 3<>/dev/tcp/127.0.0.1/${ADMIN_PORT}) 2>/dev/null; then
    exit 0
fi

exec python3 "${APP_ROOT}/admin/admin_server.py"
