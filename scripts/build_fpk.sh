#!/bin/bash
# ============================================================================
# fnSoar fpk 打包脚本（官方 fnpack 版）
# 用法: ./scripts/build_fpk.sh [版本号]
# 示例: ./scripts/build_fpk.sh 1.0.81   （缺省取 fnpack/manifest 中的 version）
#
# 产出三个安装包（位于 dist/）：
#   dist/fnSoar<版本>.fpk          通用包   —— 内置 amd64 + arm64 双内核（默认推荐）platform=all
#   dist/fnSoar<版本>-amd64.fpk    仅 x86_64 —— 只含 amd64 内核，体积更小      platform=x86
#   dist/fnSoar<版本>-arm64.fpk    仅 arm64  —— 只含 arm64 内核，体积更小      platform=arm
#
# 仓库布局（参考 Clash-for-fnos 分类整理）：
#   backend/      后端：Python admin 服务
#   frontend/     前端：admin 界面 + zashboard/metacubexd 面板
#   fnpack/       fnOS 打包源：manifest/cmd/config/wizard/ui + 运行期脚本与默认配置
#   resources/    按架构分放的 Mihomo 内核（core/x86、core/arm）
#   scripts/      构建脚本
#   dist/         构建产物与暂存目录
#
# 包结构为官方规范布局（由 fnpack build 校验并打包）：
#   {manifest, cmd/, config/, wizard/, ICON.PNG, ICON_256.PNG, app/...}
# 安装时 app/ 内容落到应用 target 目录，布局与历史版本完全一致：
#   app/{admin,bin,dashboard,default-config,ui}
# bin/mihomo 按 uname -m 自动选内核。
# ============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MANIFEST_VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "${PROJECT_ROOT}/fnpack/manifest" | head -n1 | tr -d '[:space:]')"
VERSION="${1:-${MANIFEST_VERSION}}"
if [ -z "${VERSION}" ]; then
    echo "错误: 未指定版本号且无法从 fnpack/manifest 解析 version" >&2
    exit 1
fi

BUILD_DIR="${PROJECT_ROOT}/dist/tmp_build"
OUT_DIR="${PROJECT_ROOT}/dist"
BACKEND_ADMIN="${PROJECT_ROOT}/backend/admin"
FRONTEND_ADMIN="${PROJECT_ROOT}/frontend/admin"
FRONTEND_DASHBOARD="${PROJECT_ROOT}/frontend/dashboard"
FNPACK_DIR="${PROJECT_ROOT}/fnpack"
CORE_DIR="${PROJECT_ROOT}/resources/core"

APPNAME="$(sed -n 's/^appname[[:space:]]*=[[:space:]]*//p' "${FNPACK_DIR}/manifest" | head -n1 | tr -d '[:space:]')"

echo "=== fnSoar fpk build v${VERSION} (fnpack) ==="

# 0. 架构校验：通用包需要双内核，单独包需要对应内核
echo "[0/6] 校验内核二进制..."
for pair in "x86/mihomo-amd64.real:amd64" "arm/mihomo-arm64.real:arm64"; do
    name="${pair%%:*}"
    if [ ! -f "${CORE_DIR}/${name}" ]; then
        echo "错误: 缺少 resources/core/${name}（${pair##*:} 内核）" >&2
        echo "提示: 该文件默认不入库（见 .gitignore），请从既有安装包或官方 Release 获取后放入 resources/core/<arch>/" >&2
        exit 1
    fi
done
file "${CORE_DIR}/x86/mihomo-amd64.real" "${CORE_DIR}/arm/mihomo-arm64.real" | sed 's/^/  /'

if [ -z "${APPNAME}" ]; then
    echo "错误: 无法从 manifest 解析 appname" >&2
    exit 1
fi

# 更新源 manifest/app.json 版本号（仅一次）
echo "[1/6] 更新源版本号..."
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "${FNPACK_DIR}/app.json"
sed -i "s/^version[[:space:]]*=[[:space:]]*[0-9.]*/version               = ${VERSION}/" "${FNPACK_DIR}/manifest"

mkdir -p "${BUILD_DIR}/out"

# 各包变体：suffix -> (manifest platform 声明, 是否含 amd64 内核, 是否含 arm64 内核)
build_variant() {
    local suffix="$1"        # ""=通用, "-amd64", "-arm64"
    local platform="$2"      # manifest platform 声明: all / x86 / arm
    local has_amd64="$3"
    local has_arm64="$4"

    local STAGE="${BUILD_DIR}/stage${suffix}"
    local OUTPUT="${OUT_DIR}/fnSoar${VERSION}${suffix}.fpk"

    echo ""
    echo "── 构建 ${OUTPUT} (platform: ${platform}) ──"

    # 2. 组装 stage（官方规范布局：manifest/cmd/config/wizard + 图标在顶层，应用文件在 app/）
    echo "  [2/6] 组装 stage..."
    rm -rf "${STAGE}"
    mkdir -p "${STAGE}"
    cp -f "${FNPACK_DIR}/manifest" "${STAGE}/manifest"
    cp -a "${FNPACK_DIR}/cmd"    "${STAGE}/cmd"
    cp -a "${FNPACK_DIR}/config" "${STAGE}/config"
    cp -a "${FNPACK_DIR}/wizard" "${STAGE}/wizard"
    cp -f "${FNPACK_DIR}/ICON.PNG"     "${STAGE}/ICON.PNG"
    cp -f "${FNPACK_DIR}/ICON_256.PNG" "${STAGE}/ICON_256.PNG"
    cp -f "${FNPACK_DIR}/favicon.png"  "${STAGE}/favicon.png"
    # app.json 的 icon 字段引用 favicon.png，需在 app/ 根也放一份
    mkdir -p "${STAGE}/app"
    cp -f "${FNPACK_DIR}/favicon.png"  "${STAGE}/app/favicon.png"
    # 写入该包的 platform / version 声明
    sed -i "s/^platform[[:space:]]*=.*/platform              = ${platform}/" "${STAGE}/manifest"
    sed -i "s/^version[[:space:]]*=[[:space:]]*[0-9.]*/version               = ${VERSION}/" "${STAGE}/manifest"

    # 3. 组装 app 内容（安装后的运行目录布局：admin/bin/dashboard/default-config/ui）
    echo "  [3/6] 组装 app${suffix} 内容..."
    mkdir -p "${STAGE}/app/admin" "${STAGE}/app/bin"
    # 3a. 后端（Python admin 服务）+ admin 前端界面：运行期同居 admin/ 目录
    cp -a "${BACKEND_ADMIN}/."       "${STAGE}/app/admin/"
    cp -a "${FRONTEND_ADMIN}/."      "${STAGE}/app/admin/"
    # 3b. 运行期启动脚本（bin/mihomo 架构包装器、bin/engine-start 等）
    cp -a "${FNPACK_DIR}/app/bin/."  "${STAGE}/app/bin/"
    # 3c. 按架构拷贝引擎二进制（resources/core -> bin/mihomo-<arch>.real）
    if [ "$has_amd64" = "1" ]; then cp -f "${CORE_DIR}/x86/mihomo-amd64.real" "${STAGE}/app/bin/"; fi
    if [ "$has_arm64" = "1" ]; then cp -f "${CORE_DIR}/arm/mihomo-arm64.real" "${STAGE}/app/bin/"; fi
    # 3d. 面板（第三方构建产物）、默认配置与桌面入口
    if [ -d "${FRONTEND_DASHBOARD}" ]; then
        cp -a "${FRONTEND_DASHBOARD}" "${STAGE}/app/dashboard"
    else
        echo "    ⚠ 未找到 frontend/dashboard/（面板为第三方构建产物，需先放入）" >&2
    fi
    cp -a "${FNPACK_DIR}/app/default-config" "${STAGE}/app/default-config"
    cp -a "${FNPACK_DIR}/ui"                "${STAGE}/app/ui"
    chmod -R a+rX "${STAGE}/app"

    # 4. fnpack 打包（自动校验必要文件并生成 fpk；产物写到 CWD）
    echo "  [4/6] fnpack build..."
    (cd "${BUILD_DIR}/out" && rm -f "${APPNAME}.fpk" && fnpack build --directory "${STAGE}")
    mv -f "${BUILD_DIR}/out/${APPNAME}.fpk" "${OUTPUT}"

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
ls -lh "${OUT_DIR}"/fnSoar${VERSION}*.fpk | sed 's/^/  /'
