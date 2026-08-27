#!/bin/bash
# ============================================================================
# fnSoar fpk 打包脚本（官方 fnpack 版）
# 用法: ./build_fpk.sh [版本号]
# 示例: ./build_fpk.sh 1.0.75
#
# 产出三个安装包：
#   fnSoar<版本>.fpk          通用包   —— 内置 amd64 + arm64 双内核（默认推荐）platform=all
#   fnSoar<版本>-amd64.fpk    仅 x86_64 —— 只含 amd64 内核，体积更小      platform=x86
#   fnSoar<版本>-arm64.fpk    仅 arm64  —— 只含 arm64 内核，体积更小      platform=arm
#
# 包结构为官方规范布局（由 fnpack build 校验并打包）：
#   {manifest, cmd/, config/, wizard/, ICON.PNG, ICON_256.PNG, app/...}
# 安装时 app/ 内容落到应用 target 目录，bin/mihomo 按 uname -m 自动选内核。
# ============================================================================
set -e

VERSION="${1:-1.0.75}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/tmp_build"
BIN_DIR="${SCRIPT_DIR}/app/bin"
APPNAME="$(sed -n 's/^appname[[:space:]]*=[[:space:]]*//p' "${SCRIPT_DIR}/manifest" | head -n1 | tr -d '[:space:]')"

echo "=== fnSoar fpk build v${VERSION} (fnpack) ==="

# 0. 架构校验：通用包需要双内核，单独包需要对应内核
echo "[0/6] 校验内核二进制..."
for pair in "mihomo-amd64.real:amd64" "mihomo-arm64.real:arm64"; do
    name="${pair%%:*}"
    if [ ! -f "${BIN_DIR}/${name}" ]; then
        echo "错误: 缺少 ${name}（${pair##*:} 内核）" >&2
        exit 1
    fi
done
file "${BIN_DIR}/mihomo-amd64.real" "${BIN_DIR}/mihomo-arm64.real" | sed 's/^/  /'

if [ -z "${APPNAME}" ]; then
    echo "错误: 无法从 manifest 解析 appname" >&2
    exit 1
fi

# 更新源 manifest/app.json 版本号（仅一次）
echo "[1/6] 更新源版本号..."
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "${SCRIPT_DIR}/app.json"
sed -i "s/^version[[:space:]]*=[[:space:]]*[0-9.]*/version               = ${VERSION}/" "${SCRIPT_DIR}/manifest"

OUT_DIR="${BUILD_DIR}/out"
mkdir -p "${OUT_DIR}"

# 各包变体：suffix -> (stage 目录, manifest platform 声明, 是否含 amd64 内核, 是否含 arm64 内核)
build_variant() {
    local suffix="$1"        # ""=通用, "-amd64", "-arm64"
    local platform="$2"      # manifest platform 声明: all / x86 / arm
    local has_amd64="$3"
    local has_arm64="$4"

    local STAGE="${BUILD_DIR}/stage${suffix}"
    local OUTPUT="${SCRIPT_DIR}/fnSoar${VERSION}${suffix}.fpk"

    echo ""
    echo "── 构建 ${OUTPUT} (platform: ${platform}) ──"

    # 2. 组装 stage（官方规范布局：manifest/cmd/config/wizard + 图标在顶层，应用文件在 app/）
    echo "  [2/6] 组装 stage..."
    rm -rf "${STAGE}"
    mkdir -p "${STAGE}"
    cp -f "${SCRIPT_DIR}/manifest" "${STAGE}/manifest"
    cp -a "${SCRIPT_DIR}/cmd"    "${STAGE}/cmd"
    cp -a "${SCRIPT_DIR}/config" "${STAGE}/config"
    cp -a "${SCRIPT_DIR}/wizard" "${STAGE}/wizard"
    cp -f "${SCRIPT_DIR}/ICON.PNG"     "${STAGE}/ICON.PNG"
    cp -f "${SCRIPT_DIR}/ICON_256.PNG" "${STAGE}/ICON_256.PNG"
    # 写入该包的 platform / version 声明
    sed -i "s/^platform[[:space:]]*=.*/platform              = ${platform}/" "${STAGE}/manifest"
    sed -i "s/^version[[:space:]]*=[[:space:]]*[0-9.]*/version               = ${VERSION}/" "${STAGE}/manifest"

    # 3. 准备 app 内容（整目录复制；单独包剔除另一架构内核）
    echo "  [3/6] 准备 app${suffix} 内容..."
    mkdir -p "${STAGE}/app"
    cp -a "${SCRIPT_DIR}/app/." "${STAGE}/app/"
    chmod -R a+rX "${STAGE}/app"
    if [ "$has_amd64" = "0" ]; then rm -f "${STAGE}/app/bin/mihomo-amd64.real"; fi
    if [ "$has_arm64" = "0" ]; then rm -f "${STAGE}/app/bin/mihomo-arm64.real"; fi

    # 4. fnpack 打包（自动校验必要文件并生成 fpk；产物写到 CWD）
    echo "  [4/6] fnpack build..."
    (cd "${OUT_DIR}" && rm -f "${APPNAME}.fpk" && fnpack build --directory "${STAGE}")
    mv -f "${OUT_DIR}/${APPNAME}.fpk" "${OUTPUT}"

    # 5. 验证
    echo "  [5/6] 验证..."
    SIZE=$(ls -lh "${OUTPUT}" | awk '{print $5}')
    echo "    → ${OUTPUT} (${SIZE})"
    echo "    → 包内 manifest platform/version:"
    tar xzOf "${OUTPUT}" manifest 2>/dev/null | grep -E '^(platform|version)' | sed 's/^/      /'
    echo "    → 包内顶层:"$(tar tzf "${OUTPUT}" | grep -vE '/|app\.tgz' | tr '\n' ' ')
    echo "    → 包内内核:"
    if [ "$has_amd64" = "1" ]; then
        tar xzOf "${OUTPUT}" app.tgz 2>/dev/null | gunzip -c 2>/dev/null | tar t 2>/dev/null | grep -q "mihomo-amd64.real" \
            && echo "      ✓ bin/mihomo-amd64.real" || echo "      ✗ 缺少 bin/mihomo-amd64.real"
    fi
    if [ "$has_arm64" = "1" ]; then
        tar xzOf "${OUTPUT}" app.tgz 2>/dev/null | gunzip -c 2>/dev/null | tar t 2>/dev/null | grep -q "mihomo-arm64.real" \
            && echo "      ✓ bin/mihomo-arm64.real" || echo "      ✗ 缺少 bin/mihomo-arm64.real"
    fi
}

build_variant ""         all 1 1   # 通用（双内核）
build_variant "-amd64"   x86 1 0   # 仅 x86_64
build_variant "-arm64"   arm 0 1   # 仅 arm64

echo ""
echo "=== 打包完成 ==="
ls -lh "${SCRIPT_DIR}"/fnSoar${VERSION}*.fpk | sed 's/^/  /'
