#!/bin/bash
# Rapfi 云端桥接 —— 在 GitHub Codespaces 里一键部署
# 用法: bash setup.sh
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$HOME"

echo "① 安装依赖…"
sudo apt-get update -qq
sudo apt-get install -y -qq p7zip-full python3-pip curl >/dev/null 2>&1
pip install --quiet websockets

echo "② 下载 Rapfi 官方发行包（约 37MB）…"
if [ ! -f /tmp/rapfi.7z ]; then
  curl -sL -o /tmp/rapfi.7z \
    https://github.com/dhbloo/rapfi/releases/download/250615/Rapfi-engine.7z
fi
ls -la /tmp/rapfi.7z | awk '{print "   已下载 "$5" 字节"}'

echo "③ 解压…"
rm -rf /tmp/rapfiex && mkdir -p /tmp/rapfiex
7z x -y -o/tmp/rapfiex /tmp/rapfi.7z >/dev/null

echo "④ 按 CPU 指令集选最快的引擎版本…"
mkdir -p "$HOME/rapfi"
cp /tmp/rapfiex/config.toml "$HOME/rapfi/"
cp /tmp/rapfiex/*.bin.lz4 "$HOME/rapfi/" 2>/dev/null || true
cp /tmp/rapfiex/model*.bin "$HOME/rapfi/" 2>/dev/null || true

FLAGS=$(grep -m1 '^flags' /proc/cpuinfo || true)
PICK="avx2"
if echo "$FLAGS" | grep -qwE "avx512_vnni|avx512vnni"; then PICK="avx512vnni"
elif echo "$FLAGS" | grep -qw avx512f;    then PICK="avx512"
elif echo "$FLAGS" | grep -qwE "avx_vnni|avxvnni"; then PICK="avxvnni"
elif echo "$FLAGS" | grep -qw avx2;       then PICK="avx2"
else PICK="sse"; fi
echo "   CPU 支持 → 选用 $PICK 版本（官方推荐序：avx512vnni > avx512 > avxvnni > avx2 > sse）"
cp "/tmp/rapfiex/pbrain-rapfi-linux-clang-$PICK" "$HOME/rapfi/pbrain-rapfi"
chmod +x "$HOME/rapfi/pbrain-rapfi"

# 云端核多，线程数按 CPU 核数走（上限 16，避免超线程反而变慢）
CORES=$(nproc)
THREADS=$(( CORES > 16 ? 16 : CORES ))
sed -i "s/^default_thread_num = .*/default_thread_num = $THREADS/" "$HOME/rapfi/config.toml" || true
echo "   检测到 $CORES 核，线程数设为 $THREADS"

echo "⑤ 放置桥接脚本…"
if [ -f "$SCRIPT_DIR/bridge.py" ]; then
  cp "$SCRIPT_DIR/bridge.py" "$HOME/rapfi/bridge.py"
else
  echo "   ⚠️ 找不到 bridge.py，请把它和本脚本放一起"
  exit 1
fi

echo "⑥ 本地自检（先确认引擎能跑）…"
cd "$HOME/rapfi"
echo -e "START 15\nINFO thread_num $THREADS\nINFO rule 0\nYXBOARD\n7,7,1\nDONE\nYXNBEST 1" \
  | timeout 25 ./pbrain-rapfi 2>/dev/null | tail -5
echo "   以上若出现「深度/Depth」或坐标，说明引擎正常 ✓"

echo "⑦ 启动桥接…"
nohup python3 bridge.py > /tmp/bridge.log 2>&1 &
for i in $(seq 1 30); do
  sleep 1
  grep -q "桥接就绪" /tmp/bridge.log 2>/dev/null && break
done
if grep -q "桥接就绪" /tmp/bridge.log; then
  echo "✅ 桥接已就绪（端口 8766）"
  echo "   手机端连接地址：wss://<你的codespace名>-8766.app.github.dev"
  echo "   （端口可见性设为 public 后即可从外网连）"
else
  echo "❌ 启动失败，日志："
  tail -15 /tmp/bridge.log
  exit 1
fi
