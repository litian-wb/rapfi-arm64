#!/bin/bash
# 云端一键启动：Rapfi 桥接 + Cloudflare 隧道
# 用法: bash start.sh          （首次请先跑 setup.sh）
#       bash start.sh stop     （停止，省免费额度）
set -u
cd "$HOME/rapfi" 2>/dev/null || { echo "❌ 还没部署，请先跑 setup.sh"; exit 1; }

stop_all() {
  pkill -f 'cloudflared tunnel' 2>/dev/null || true
  for d in /proc/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    c=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
    case "$c" in *bridge.py*|*pbrain-rapfi*) kill -9 "${d#/proc/}" 2>/dev/null ;; esac
  done
  sleep 2
}

if [ "${1:-}" = "stop" ]; then
  stop_all
  echo "✅ 已停止（Codespace 本身也要在 GitHub 网页上停掉，否则继续扣额度）"
  exit 0
fi

echo "① 停止旧进程…"
stop_all

echo "② 启动桥接…"
# setsid：脱离 SSH 会话，否则会话一断就被杀
setsid nohup python3 bridge.py > /tmp/bridge.log 2>&1 < /dev/null &
for i in $(seq 1 30); do
  sleep 1
  grep -q "桥接就绪" /tmp/bridge.log 2>/dev/null && break
done
grep -q "桥接就绪" /tmp/bridge.log || { echo "❌ 桥接启动失败："; tail -10 /tmp/bridge.log; exit 1; }
echo "   ✅ 桥接就绪"

echo "③ 启动 Cloudflare 隧道（给公网地址）…"
if ! command -v cloudflared >/dev/null 2>&1; then
  curl -sL -o /tmp/cloudflared \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x /tmp/cloudflared
  sudo mv /tmp/cloudflared /usr/local/bin/cloudflared
fi
setsid nohup cloudflared tunnel --url http://127.0.0.1:8766 \
  --no-autoupdate > /tmp/cf.log 2>&1 < /dev/null &

URL=""
for i in $(seq 1 40); do
  sleep 2
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /tmp/cf.log 2>/dev/null | head -1)
  [ -n "$URL" ] && break
done

if [ -z "$URL" ]; then
  echo "❌ 没拿到隧道地址，日志："
  tail -15 /tmp/cf.log
  exit 1
fi

echo "   ✅ 公网地址: $URL"
echo
echo "════════════════════════════════════"
echo "  手机端要填的地址（引擎地址按钮）："
echo "  ${URL/https:/wss:}"
echo "════════════════════════════════════"

# 自测
python3 - "$URL" <<'PYEOF' 2>/dev/null || true
import asyncio, json, sys, websockets
url = sys.argv[1].replace('https://','wss://')
async def m():
    try:
        async with websockets.connect(url, open_timeout=25) as ws:
            await ws.send(json.dumps({'cmd':'ping'}))
            print('   自测: 连接成功 →', await asyncio.wait_for(ws.recv(), 15))
    except Exception as e:
        print('   自测失败:', type(e).__name__)
asyncio.run(m())
PYEOF
