#!/bin/sh
# 用法：GH_PAT=<你的token> sh push_and_fetch.sh
# 全程只引用 $GH_PAT，不打印其内容。
set -e

[ -n "$GH_PAT" ] || { echo "❌ 缺少 GH_PAT 环境变量（GitHub token）"; exit 1; }

REPO_NAME="${REPO_NAME:-rapfi-arm64}"
SRC="/var/minis/workspace/rapfi-ci"
OUT="/var/minis/workspace/rapfi_native"

echo "$GH_PAT" | gh auth login --with-token
OWNER="$(gh api user -q .login)"
echo "→ 账号 $OWNER / 仓库 $REPO_NAME"

cd "$SRC"
git init -q 2>/dev/null || true
git -c user.email=minis@local -c user.name=minis add -A
git -c user.email=minis@local -c user.name=minis commit -qm "ci: build rapfi for aarch64 musl" 2>/dev/null || true
git branch -M main

if gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
  echo "→ 仓库已存在，推送更新"
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
  git push -q -u origin main --force
else
  echo "→ 创建公开仓库并推送（公开仓库才能用免费 ARM 跑机器人）"
  gh repo create "$OWNER/$REPO_NAME" --public --source . --push
fi

echo "→ 等待构建启动…"
sleep 20
gh run list --repo "$OWNER/$REPO_NAME" --limit 3

RUN_ID="$(gh run list --repo "$OWNER/$REPO_NAME" --workflow build.yml --limit 1 --json databaseId -q '.[0].databaseId')"
echo "→ 跟踪构建 $RUN_ID（约 5~15 分钟）"
if ! gh run watch "$RUN_ID" --repo "$OWNER/$REPO_NAME" --exit-status; then
  echo "❌ 构建失败，失败日志："
  gh run view "$RUN_ID" --repo "$OWNER/$REPO_NAME" --log-failed | tail -60
  exit 1
fi

echo "→ 下载产物"
rm -rf "$OUT"; mkdir -p "$OUT"
gh run download "$RUN_ID" --repo "$OWNER/$REPO_NAME" -n rapfi-aarch64-musl -D "$OUT"
chmod +x "$OUT"/* 2>/dev/null || true

echo "→ 产物信息"
ls -la "$OUT"
file "$OUT"/* 2>/dev/null || true
echo "→ 动态依赖（应为空）"
readelf -d "$OUT"/* 2>/dev/null | grep NEEDED || echo "无动态依赖 ✅ 可直接在本机运行"
