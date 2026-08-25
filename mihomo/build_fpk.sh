#!/bin/bash
# ============================================================================
# fnSoar fpk 打包脚本
# 用法: ./build_fpk.sh [版本号]
# 示例: ./build_fpk.sh 1.0.54
# ============================================================================
set -e

VERSION="${1:-1.0.53}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/tmp_build"
STAGE_DIR="${BUILD_DIR}/stage"
OUTPUT="${SCRIPT_DIR}/mihomo${VERSION}.fpk"

echo "=== fnSoar fpk build v${VERSION} ==="

# 1. 更新版本号
echo "[1/5] 更新版本号..."
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "${SCRIPT_DIR}/app.json"
sed -i "s/^version[[:space:]]*=[[:space:]]*[0-9.]*/version               = ${VERSION}/" "${SCRIPT_DIR}/manifest"
sed -i "s/^version[[:space:]]*=[[:space:]]*[0-9.]*/version               = ${VERSION}/" "${STAGE_DIR}/manifest"

# 2. 打包 app.tgz（关键：不带 app/ 前缀，直接用 -C app/ .）
echo "[2/5] 打包 app.tgz..."
mkdir -p "${STAGE_DIR}"
# 确保所有文件权限可读，避免解压后权限为 000
chmod -R a+rX "${SCRIPT_DIR}/app/"
tar cf "${BUILD_DIR}/app.tar" -C "${SCRIPT_DIR}/app/" .
gzip -f -c "${BUILD_DIR}/app.tar" > "${STAGE_DIR}/app.tgz"

# 3. 打包 fpk.tar
echo "[3/5] 打包 fpk.tar..."
tar cf "${BUILD_DIR}/fpk.tar" -C "${BUILD_DIR}" stage/

# 4. 压缩为 .fpk
echo "[4/5] 压缩 fpk..."
gzip -f -c "${BUILD_DIR}/fpk.tar" > "${OUTPUT}"

# 5. 验证
echo "[5/5] 验证..."
SIZE=$(ls -lh "${OUTPUT}" | awk '{print $5}')
echo "  → ${OUTPUT} (${SIZE})"
echo "  → app.tgz 根目录:"
gunzip -c "${STAGE_DIR}/app.tgz" | tar tf - | grep -v "/" | head -5

echo ""
echo "=== 打包完成: ${OUTPUT} ==="