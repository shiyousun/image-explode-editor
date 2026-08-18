#!/usr/bin/env bash
# 图片炸开编辑器 —— Linux 服务器后台启动
# 用法：./run_linux.sh [端口]   默认 8770
set -euo pipefail

cd "$(dirname "$0")"
PORT="${1:-8770}"
PY="./venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "缺少虚拟环境，正在创建（约 1-3 分钟）…"
  PY3="$(command -v python3.12 || command -v python3.11 || command -v python3)"
  "$PY3" -m venv venv
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple/
fi

# 只停自己这个端口上的旧实例，不碰服务器上别人的进程
if [ -f .server.pid ] && kill -0 "$(cat .server.pid)" 2>/dev/null; then
  echo "停掉旧实例 PID $(cat .server.pid)"
  kill "$(cat .server.pid)" 2>/dev/null || true
  sleep 1
fi

# 绑 0.0.0.0，否则只有服务器本机能访问
nohup "$PY" backend/server.py --host 0.0.0.0 --port "$PORT" > server.log 2>&1 &
echo $! > .server.pid
sleep 4

for _ in $(seq 1 20); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)"
  [ "$code" = "200" ] && break
  sleep 1
done

if [ "${code:-}" = "200" ]; then
  echo "启动成功 PID $(cat .server.pid)，监听 0.0.0.0:$PORT"
  echo "访问：http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT/"
else
  echo "启动失败（HTTP ${code:-无响应}），日志尾部："
  tail -25 server.log
  exit 1
fi
