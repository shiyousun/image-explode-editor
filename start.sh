#!/usr/bin/env bash
# 图片炸开编辑器 —— 一键启动
# 用法：./start.sh [端口]        默认 8770，后台跑并自动开浏览器
#       ./start.sh --fg [端口]   前台跑，日志直接打在终端里（调试用）
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "--fg" ]; then
  shift
  PORT="${1:-8770}"
  PY="./venv/bin/python"
  if [ ! -x "$PY" ]; then
    echo "首次运行：正在创建虚拟环境并安装依赖（约 1-3 分钟）…"
    PY3="$(command -v python3.12 || command -v python3)"
    "$PY3" -m venv venv
    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q -r requirements.txt
    echo "依赖安装完成。"
  fi
  if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "端口 $PORT 已被占用，正在停掉旧进程…"
    lsof -ti "tcp:$PORT" -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
  echo "启动中（前台）…浏览器打开 http://127.0.0.1:$PORT/"
  exec "$PY" backend/server.py --port "$PORT"
fi

exec bash scripts/mac_launch.sh "$@"
