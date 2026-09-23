#!/bin/bash
# Codespace 每次启动时自动执行（由 devcontainer 的 postStartCommand 调用）
# 目的：机器一开，引擎自动就位，用户什么都不用做。
set -u

log() { echo "[boot] $*"; }

# 首次没有引擎 → 先跑完整安装
if [ ! -x "$HOME/rapfi/pbrain-rapfi" ]; then
  log "首次启动，执行完整安装…"
  bash /workspaces/rapfi-arm64/cloud/setup.sh 2>&1 | tail -20
fi

cd "$HOME/rapfi" || { log "❌ 找不到 ~/rapfi"; exit 1; }

# 清掉旧进程（按 /proc 取 PID，iSH/Codespace 上 pkill 都不可靠）
for d in /proc/[0-9]*; do
  [ -r "$d/cmdline" ] || continue
  c=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
  case "$c" in
    *bridge.py*|*pbrain-rapfi*) kill -9 "${d#/proc/}" 2>/dev/null ;;
  esac
done
sleep 2

# 拉起桥接
setsid nohup python3 bridge.py > /tmp/bridge.log 2>&1 < /dev/null &
for i in $(seq 1 40); do
  sleep 1
  grep -q "桥接就绪" /tmp/bridge.log 2>/dev/null && break
done

if grep -q "桥接就绪" /tmp/bridge.log; then
  log "✅ 引擎就绪，监听 8766"
else
  log "❌ 启动失败："
  tail -10 /tmp/bridge.log
  exit 1
fi
