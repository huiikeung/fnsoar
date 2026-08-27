#!/bin/bash
# 发布 fnSoar v1.0.76 Release（需先完成 gh 登录）
# 用法: 先执行  HOME=/root gh auth login --hostname github.com
#       再执行  bash release_v1.0.76.sh
set -e
cd "$(dirname "$0")"

export HOME=/root

echo "== 检查 gh 认证..."
gh auth status >/dev/null 2>&1 || { echo "gh 未登录，请先: HOME=/root gh auth login --hostname github.com"; exit 1; }

echo "== 检查附件..."
for f in fnSoar1.0.76.fpk fnSoar1.0.76-amd64.fpk fnSoar1.0.76-arm64.fpk; do
    [ -f "$f" ] || { echo "缺少 $f（先运行 ./build_fpk.sh 1.0.76）"; exit 1; }
done

echo "== 创建 Release v1.0.76 并上传附件..."
gh release create v1.0.76 \
    fnSoar1.0.76.fpk \
    fnSoar1.0.76-amd64.fpk \
    fnSoar1.0.76-arm64.fpk \
    --title "fnSoar v1.0.76" \
    --notes-file RELEASE_NOTES_v1.0.76.md

echo "== 完成: $(gh release view v1.0.76 --json url -q .url)"
