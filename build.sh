#!/bin/bash
# fnSoar App Build Script
# Copies app files to deployment directory and restarts the service

set -e

APP_NAME="fnnas.fnsoar"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="/vol1/@appcenter/${APP_NAME}"
DATA_DIR="/vol1/@appdata/${APP_NAME}"

echo "=== fnSoar App Build ==="
echo "Source: ${SOURCE_DIR}"
echo "Dest:   ${DEST_DIR}"

# Stop old service if running
if [ -f "${DEST_DIR}/app/admin/admin_server.py" ]; then
    echo "Stopping old service..."
    cd "${DEST_DIR}" && appcenter-cli stop "${APP_NAME}" 2>/dev/null || true
    sleep 2
fi

# Create directories
mkdir -p "${DEST_DIR}/app/admin"
mkdir -p "${DEST_DIR}/bin"
mkdir -p "${DATA_DIR}"

# Copy admin files
echo "Copying admin files..."
cp -f "${SOURCE_DIR}/app/admin/index.html" "${DEST_DIR}/app/admin/"
cp -f "${SOURCE_DIR}/app/admin/admin_server.py" "${DEST_DIR}/app/admin/"
cp -f "${SOURCE_DIR}/app/admin/host_transparent.sh" "${DEST_DIR}/app/admin/"
chmod +x "${DEST_DIR}/app/admin/host_transparent.sh"
cp -f "${SOURCE_DIR}/app.json" "${DEST_DIR}/"
cp -f "${SOURCE_DIR}/favicon.png" "${DEST_DIR}/" 2>/dev/null || true

# Copy mihomo binary if exists
if [ -f "${SOURCE_DIR}/bin/mihomo" ]; then
    cp -f "${SOURCE_DIR}/bin/mihomo" "${DEST_DIR}/bin/mihomo"
    chmod +x "${DEST_DIR}/bin/mihomo"
fi

# Copy engine starter (panel daemon manages the engine as a child)
if [ -f "${SOURCE_DIR}/app/bin/engine-start" ]; then
    cp -f "${SOURCE_DIR}/app/bin/engine-start" "${DEST_DIR}/app/bin/engine-start"
    chmod +x "${DEST_DIR}/app/bin/engine-start"
fi

# Copy config if not exists
if [ ! -f "${DATA_DIR}/config.yaml" ]; then
    if [ -f "${SOURCE_DIR}/config.yaml" ]; then
        cp -f "${SOURCE_DIR}/config.yaml" "${DATA_DIR}/config.yaml"
    fi
fi

echo "Build complete. Starting service..."
cd "${DEST_DIR}" && appcenter-cli start "${APP_NAME}"
sleep 3
echo "Done! App available at http://127.0.0.1:9099/"