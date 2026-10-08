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
# 说明：frontend/dashboard 与 resources/core 按 .gitignore 设计不入库。
#       打包时若缺失，会自动从 GitHub Release 下载最新版
#       （确保任何人 clone 仓库后都能直接打出完整安装包）。
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

# 0. 内核/面板补齐：resources/core 与 frontend/dashboard 按 .gitignore 设计
#    不入库，缺失时自动从 GitHub Release 下载最新版，确保任何人 clone 仓库后
#    都能直接打出完整安装包。
echo "[0/6] 校验内核与面板（缺失自动下载）..."
ensure_cores() {
    local api="https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
    local miss_x86=0 miss_arm=0
    if [ ! -f "${CORE_DIR}/x86/mihomo-amd64.real" ]; then miss_x86=1; fi
    if [ ! -f "${CORE_DIR}/arm/mihomo-arm64.real" ]; then miss_arm=1; fi
    if [ "$miss_x86" = "0" ] && [ "$miss_arm" = "0" ]; then
        echo "  ✓ 内核已存在，跳过下载"
        return 0
    fi
    echo "  resources/core 不完整 → 自动从 mihomo GitHub Release 下载最新内核..."
    local tmp; tmp="$(mktemp -d)"

    # 下载并安装单个架构内核：$1=arch 目录(x86/arm) $2=目标文件名 $3=首选资产正则 $4=备选资产正则
    _install_core() {
        local arch="$1" binname="$2" primary="$3" fallback="$4"
        local dest="${CORE_DIR}/${arch}/${binname}"
        if [ -f "${dest}" ]; then
            echo "    ✓ ${arch} 内核已存在"
            return 0
        fi
        local url tag
        read -r url tag <<<"$(curl -sL --retry 6 --retry-delay 3 -m 40 "${api}" 2>/dev/null | python3 -c "
import sys, json, re
try:
    d = json.load(sys.stdin)
    assets = d.get('assets', [])
    for pat in (r'${primary}', r'${fallback}'):
        rx = re.compile(pat)
        hit = next((a for a in assets if rx.match(a.get('name', ''))), None)
        if hit:
            print(hit['browser_download_url'], d.get('tag_name', ''))
            break
except Exception:
    pass" 2>/dev/null)"
        if [ -z "${url}" ]; then
            echo "    ⚠ ${arch}: 无法从 GitHub API 获取下载地址，跳过（请手动放入 resources/core/${arch}/）" >&2
            return 0
        fi
        echo "    ↓ ${arch} 内核 ${tag}（${url##*/}）"
        if ! curl -fsL --retry 5 --retry-delay 3 -m 600 -o "${tmp}/core-${arch}.gz" "${url}"; then
            echo "    ⚠ ${arch}: 下载失败，跳过（请手动放入 resources/core/${arch}/）" >&2
            return 0
        fi
        mkdir -p "${CORE_DIR}/${arch}"
        if ! gunzip -c "${tmp}/core-${arch}.gz" > "${dest}" 2>/dev/null; then
            echo "    ⚠ ${arch}: 解压失败（非 gzip？），跳过" >&2
            rm -f "${dest}"
            return 0
        fi
        chmod 755 "${dest}"
        # 校验：本机架构匹配就直接跑 -v；跨架构构建跑不了，退而校验 ELF 机器类型
        # （e_machine：0x3E=62=x86-64，0xB7=183=aarch64）
        local want_em=62
        if [ "${arch}" = "arm" ]; then want_em=183; fi
        local got_em
        got_em="$(od -An -tu1 -j18 -N1 "${dest}" 2>/dev/null | tr -d ' ')"
        if "${dest}" -v 2>/dev/null | grep -q "Mihomo Meta"; then
            echo "    ✓ ${arch} 内核 ${tag} 就绪（可执行验证通过）"
        elif [ "${got_em}" = "${want_em}" ]; then
            echo "    ✓ ${arch} 内核 ${tag} 就绪（ELF 校验通过，跨架构构建未执行验证）"
        else
            echo "    ⚠ ${arch}: 下载的文件不是预期的 ${arch} ELF（e_machine=${got_em}），已删除" >&2
            rm -f "${dest}"
            return 0
        fi
    }

    # amd64 优先 compatible 构建（老 CPU 可运行，与 App 内更新内核同款逻辑）；
    # arm64 官方无 compatible 变体，取常规构建。文件名遵循仓库约定：
    # x86 -> mihomo-amd64.real，arm -> mihomo-arm64.real
    if [ "$miss_x86" = "1" ]; then
        _install_core "x86" "mihomo-amd64.real" '^mihomo-linux-amd64-compatible-v[0-9.]+\.gz$' '^mihomo-linux-amd64-v[0-9.]+\.gz$'
    fi
    if [ "$miss_arm" = "1" ]; then
        _install_core "arm" "mihomo-arm64.real" '^mihomo-linux-arm64-v[0-9.]+\.gz$' '^mihomo-linux-arm64-v[0-9.]+\.gz$'
    fi
    rm -rf "${tmp}"
}
ensure_cores

# 兜底校验：自动下载也失败时仍按原逻辑硬错退出
for pair in "x86/mihomo-amd64.real:amd64" "arm/mihomo-arm64.real:arm64"; do
    name="${pair%%:*}"
    if [ ! -f "${CORE_DIR}/${name}" ]; then
        echo "错误: 缺少 resources/core/${name}（${pair##*:} 内核）" >&2
        echo "提示: 自动下载失败。请手动从 GitHub Release（MetaCubeX/mihomo）获取后放入 resources/core/<arch>/" >&2
        exit 1
    fi
done
file "${CORE_DIR}/x86/mihomo-amd64.real" "${CORE_DIR}/arm/mihomo-arm64.real" | sed 's/^/  /'

# 0b. 面板补齐：frontend/dashboard 缺失时自动从 GitHub Release 下载最新版
#     （该目录设计上不入库，下载后仅落盘供本次打包，不会进入 git）
echo "[0/6] 检查面板（frontend/dashboard）..."
ensure_dashboards() {
    local dd="${FRONTEND_DASHBOARD}"
    local missing=0
    if [ ! -f "${dd}/zashboard/index.html" ]; then missing=1; fi
    if [ ! -f "${dd}/metacubexd/index.html" ]; then missing=1; fi
    if [ "$missing" = "0" ]; then
        echo "  ✓ 面板已存在，跳过下载"
        return 0
    fi
    echo "  frontend/dashboard 不完整 → 自动从 GitHub Release 下载最新面板..."
    mkdir -p "${dd}"
    local tmp; tmp="$(mktemp -d)"

    # 安装单个面板：$1=name  $2=repo  $3=asset 文件名
    _install_dash() {
        local name="$1" repo="$2" asset="$3"
        if [ -f "${dd}/${name}/index.html" ]; then
            echo "    ✓ ${name} 已存在"
            return 0
        fi
        local api="https://api.github.com/repos/${repo}/releases/latest"
        local url tag
        url="$(curl -sL --retry 6 --retry-delay 3 -m 40 "${api}" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(next(a['browser_download_url'] for a in d.get('assets', []) if a.get('name') == '${asset}'))
except Exception:
    pass" 2>/dev/null)"
        tag="$(curl -sL --retry 6 --retry-delay 3 -m 40 "${api}" 2>/dev/null | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('tag_name', ''))
except Exception:
    pass" 2>/dev/null)"
        if [ -z "${url}" ]; then
            echo "    ⚠ ${name}: 无法从 GitHub API 获取下载地址，跳过（安装后可在 App 内更新面板）" >&2
            return 0
        fi
        echo "    ↓ ${name} ${tag:-latest}（${asset}）"
        if ! curl -fsL --retry 5 --retry-delay 3 -m 300 -o "${tmp}/${name}.pkg" "${url}"; then
            echo "    ⚠ ${name}: 下载失败，跳过（安装后可在 App 内更新面板）" >&2
            return 0
        fi
        local root="${tmp}/${name}x"
        rm -rf "${root}"; mkdir -p "${root}"
        case "${asset}" in
            *.zip)
                if command -v unzip >/dev/null 2>&1; then
                    unzip -q "${tmp}/${name}.pkg" -d "${root}" || { echo "    ⚠ ${name}: 解压失败" >&2; return 0; }
                else
                    python3 -m zipfile -e "${tmp}/${name}.pkg" "${root}" || { echo "    ⚠ ${name}: 解压失败" >&2; return 0; }
                fi
                ;;
            *) tar -xzf "${tmp}/${name}.pkg" -C "${root}" || { echo "    ⚠ ${name}: 解压失败" >&2; return 0; }
                ;;
        esac
        # 定位 web 根（最浅的含 index.html 的目录）
        local webroot
        webroot="$(find "${root}" -name index.html -maxdepth 2 2>/dev/null | head -1)"
        webroot="$(dirname "${webroot:-/nonexistent}")"
        if [ ! -f "${webroot}/index.html" ]; then
            echo "    ⚠ ${name}: 包内未找到 index.html，跳过" >&2
            return 0
        fi
        rm -rf "${dd}/${name}"
        cp -a "${webroot}" "${dd}/${name}"
        echo "${tag#v}" > "${dd}/${name}/VERSION"
        echo "    ✓ ${name} ${tag:-latest} 就绪"
    }

    _install_dash "zashboard"  "Zephyruso/zashboard"  "dist-cdn-fonts.zip"
    _install_dash "metacubexd" "MetaCubeX/metacubexd" "compressed-dist.tgz"
    rm -rf "${tmp}"
}
ensure_dashboards

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
