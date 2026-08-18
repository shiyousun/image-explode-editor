#!/usr/bin/env bash
# 图片炸开编辑器 —— 停止服务
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .server.pid ]; then
  echo "没有 .server.pid，服务可能没在跑"
  exit 0
fi
PID="$(cat .server.pid)"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  sleep 1
  kill -9 "$PID" 2>/dev/null || true
  echo "已停止 PID $PID"
else
  echo "PID $PID 已不在运行"
fi
rm -f .server.pid
