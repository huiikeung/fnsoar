#!/bin/bash
# ============================================================================
# fnSoar fpk 打包脚本
# 用法: ./build_fpk.sh [版本号]
# 示例: ./build_fpk.sh 1.0.54
#
# 产出三个安装包：
#   mihomo<版本>.fpk          通用包   —— 内置 amd64 + arm64 双内核（默认推荐）
#   mihomo<版本>-amd64.fpk    仅 x86_64 —— 只含 amd64 内核，体积更小
#   mihomo<版本>-arm64.fpk    仅 arm64  —— 只含 arm64 内核，体积更小
#
# manifest 的 arch 行会按包类型分别写：x86_64 arm64 / x86_64 / arm64。
# 安装时由 app/bin/mihomo 按 uname -m 自动选择内核。
# ============================================================================
set -e

VERSION="${1:-1.0.55}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/tmp_build"
BIN_DIR="${SCRIPT_DIR}/app/bin"

echo "=== fnSoar fpk build v${VERSION} ==="

# 0. 架构校验：通用包需要双内核，单独包需要对应内核
echo "[0/9] 校验内核二进制..."
for pair in "mihomo-amd64.real:amd64" "mihomo-arm64.real:arm64"; do
    name="${pair%%:*}"
    if [ ! -f "${BIN_DIR}/${name}" ]; then
        echo "错误: 缺少 ${name}（${pair##*:} 内核）" >&2
        exit 1
    fi
done
file "${BIN_DIR}/mihomo-amd64.real" "${BIN_DIR}/mihomo-arm64.real" | sed 's/^/  /'

# 更新源 manifest/app.json 版本号（仅一次）
echo "[1/9] 更新源版本号..."
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "${SCRIPT_DIR}/app.json"
sed -i "s/^version[[:space:]]*=[[:space:]]*[0-9.]*/version               = ${VERSION}/" "${SCRIPT_DIR}/manifest"

# 各包变体：suffix -> (stage 子目录, manifest arch 声明, 是否含 amd64 内核, 是否含 arm64 内核)
build_variant() {
    local suffix="$1"    # ""=通用, "-amd64", "-arm64"
    local arch="$2"      # manifest arch 声明
    local has_amd64="$3"
    local has_arm64="$4"

    local STAGE="${BUILD_DIR}/stage"
    local APP_DIR="${BUILD_DIR}/app${suffix}"
    local OUTPUT="${SCRIPT_DIR}/mihomo${VERSION}${suffix}.fpk"

    echo ""
    echo "── 构建 ${OUTPUT} (arch: ${arch}) ──"

    # 2. 组装 stage（统一目录名，保证 fpk 顶层始终为 stage/）
    echo "  [2/9] 组装 stage..."
    rm -rf "${STAGE}" "${APP_DIR}"
    mkdir -p "${STAGE}"
    cp -f "${SCRIPT_DIR}/manifest" "${STAGE}/manifest"
    cp -a "${SCRIPT_DIR}/cmd"    "${STAGE}/cmd"
    cp -a "${SCRIPT_DIR}/config" "${STAGE}/config"
    cp -a "${SCRIPT_DIR}/ui"     "${STAGE}/ui"
    cp -a "${SCRIPT_DIR}/wizard" "${STAGE}/wizard"
    cp -f "${SCRIPT_DIR}/app/admin/icon/ICON.png"     "${STAGE}/ICON.PNG"
    cp -f "${SCRIPT_DIR}/app/admin/icon/ICON_256.png" "${STAGE}/ICON_256.PNG"
    # 写入该包的 arch 声明
    sed -i "s/^arch[[:space:]]*=.*/arch                  = ${arch}/" "${STAGE}/manifest"
    sed -i "s/^version[[:space:]]*=[[:space:]]*[0-9.]*/version               = ${VERSION}/" "${STAGE}/manifest"

    # 3. 准备 app 内容（通用包整目录复制；单独包剔除另一架构内核）
    echo "  [3/9] 准备 app${suffix} 内容..."
    cp -a "${SCRIPT_DIR}/app/" "${APP_DIR}"
    chmod -R a+rX "${APP_DIR}"
    if [ "$has_amd64" = "0" ]; then rm -f "${APP_DIR}/bin/mihomo-amd64.real"; fi
    if [ "$has_arm64" = "0" ]; then rm -f "${APP_DIR}/bin/mihomo-arm64.real"; fi

    # 4. 打包 app.tgz
    echo "  [4/9] 打包 app.tgz..."
    tar cf "${BUILD_DIR}/app${suffix}.tar" -C "${APP_DIR}" .
    gzip -f -c "${BUILD_DIR}/app${suffix}.tar" > "${STAGE}/app.tgz"

    # 5. 打包 fpk.tar（-C 取统一 stage/ 目录，保证顶层结构一致）
    echo "  [5/9] 打包 fpk.tar..."
    tar cf "${BUILD_DIR}/fpk${suffix}.tar" -C "${BUILD_DIR}" stage/

    # 6. 压缩为 .fpk
    echo "  [6/9] 压缩 fpk..."
    gzip -f -c "${BUILD_DIR}/fpk${suffix}.tar" > "${OUTPUT}"

    # 7. 验证
    echo "  [7/9] 验证..."
    SIZE=$(ls -lh "${OUTPUT}" | awk '{print $5}')
    echo "    → ${OUTPUT} (${SIZE})"
    echo "    → 包内 manifest arch:"
    tar xOf "${BUILD_DIR}/fpk${suffix}.tar" "stage/manifest" 2>/dev/null | grep -E '^(arch|version)' | sed 's/^/      /'
    echo "    → 包内内核:"
    if [ "$has_amd64" = "1" ]; then
        gunzip -c "${STAGE}/app.tgz" | tar xOf - ./bin/mihomo-amd64.real >/dev/null 2>&1 && echo "      ✓ bin/mihomo-amd64.real" || echo "      ✗ 缺少 bin/mihomo-amd64.real"
    fi
    if [ "$has_arm64" = "1" ]; then
        gunzip -c "${STAGE}/app.tgz" | tar xOf - ./bin/mihomo-arm64.real >/dev/null 2>&1 && echo "      ✓ bin/mihomo-arm64.real" || echo "      ✗ 缺少 bin/mihomo-arm64.real"
    fi
}

build_variant ""         "x86_64 arm64" 1 1   # 通用（双内核）
build_variant "-amd64"   "x86_64"       1 0   # 仅 x86_64
build_variant "-arm64"   "arm64"        0 1   # 仅 arm64

echo ""
echo "=== 打包完成 ==="
ls -lh "${SCRIPT_DIR}"/mihomo${VERSION}*.fpk | sed 's/^/  /'