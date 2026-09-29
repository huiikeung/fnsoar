#!/bin/bash
# fnSoar App Build Script (dev deploy)
# Copies app files to the fnOS deployment directory and restarts the service.
# 仅用于源码树快速部署到本机 /vol1/@appcenter；正式发布请用 scripts/build_fpk.sh。

set -e

APP_NAME="fnnas.fnsoar"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST_DIR="/vol1/@appcenter/${APP_NAME}"
DATA_DIR="/vol1/@appdata/${APP_NAME}"

BACKEND_ADMIN="${PROJECT_ROOT}/backend/admin"
FRONTEND_ADMIN="${PROJECT_ROOT}/frontend/admin"
FNPACK_DIR="${PROJECT_ROOT}/fnpack"
CORE_DIR="${PROJECT_ROOT}/resources/core"

echo "=== fnSoar App Build ==="
echo "Source: ${PROJECT_ROOT}"
echo "Dest:   ${DEST_DIR}"

# Stop old service if running
if [ -f "${DEST_DIR}/admin/admin_server.py" ]; then
    echo "Stopping old service..."
    cd "${DEST_DIR}" && appcenter-cli stop "${APP_NAME}" 2>/dev/null || true
    sleep 2
fi

# Create directories
mkdir -p "${DEST_DIR}/admin" "${DEST_DIR}/bin" "${DATA_DIR}"

# Copy admin server (backend) + admin UI (frontend) — both live under admin/ at runtime
echo "Copying admin files..."
for f in admin_server.py media_unlock.py server.js host_transparent.sh start_admin.sh \
         index.html ui.html favicon.png CHANGELOG.md; do
    src="${BACKEND_ADMIN}/${f}"
    [ -f "${src}" ] || src="${FRONTEND_ADMIN}/${f}"
    [ -f "${src}" ] && cp -f "${src}" "${DEST_DIR}/admin/" || true
done
chmod +x "${DEST_DIR}/admin/host_transparent.sh" "${DEST_DIR}/admin/start_admin.sh" 2>/dev/null || true
if [ -d "${FRONTEND_ADMIN}/icon" ]; then cp -a "${FRONTEND_ADMIN}/icon" "${DEST_DIR}/admin/"; fi
if [ -d "${FRONTEND_ADMIN}/flags" ]; then cp -a "${FRONTEND_ADMIN}/flags" "${DEST_DIR}/admin/"; fi

# Copy fnOS app manifest
cp -f "${FNPACK_DIR}/app.json" "${DEST_DIR}/"

# Copy runtime launcher scripts (fnpack/app/bin -> bin/)
if [ -d "${FNPACK_DIR}/app/bin" ]; then
    cp -f "${FNPACK_DIR}/app/bin/"* "${DEST_DIR}/bin/"
    chmod +x "${DEST_DIR}/bin/"* 2>/dev/null || true
fi

# Copy mihomo engine binaries if present (gitignored; see .gitignore)
for pair in "x86/mihomo-amd64.real" "arm/mihomo-arm64.real"; do
    if [ -f "${CORE_DIR}/${pair}" ]; then
        cp -f "${CORE_DIR}/${pair}" "${DEST_DIR}/bin/$(basename "${pair}")"
        chmod +x "${DEST_DIR}/bin/$(basename "${pair}")"
    fi
done

# Copy config if not exists
if [ ! -f "${DATA_DIR}/config.yaml" ]; then
    if [ -f "${FNPACK_DIR}/app/default-config/config.yaml" ]; then
        cp -f "${FNPACK_DIR}/app/default-config/config.yaml" "${DATA_DIR}/config.yaml"
    fi
fi

echo "Build complete. Starting service..."
cd "${DEST_DIR}" && appcenter-cli start "${APP_NAME}"
sleep 3
echo "Done! App available at http://127.0.0.1:9099/"
