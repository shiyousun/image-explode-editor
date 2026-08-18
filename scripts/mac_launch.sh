#!/bin/bash
# 图片炸开编辑器 —— macOS 一键启动的公共逻辑
#
# 三个入口共用这一份：
#   启动编辑器.command      双击，开一个终端窗口，装依赖和报错都看得见
#   图片炸开编辑器.app       双击，不开终端，出问题弹原生对话框
#   start.sh                在终端里手敲
#
# 用法：mac_launch.sh [--gui] [端口]
#   --gui  没有终端可用，提示走通知中心和对话框

PORT_DEFAULT=8770
GUI=0
PORT=""
for arg in "$@"; do
  case "$arg" in
    --gui) GUI=1 ;;
    [0-9]*) PORT="$arg" ;;
  esac
done
PORT="${PORT:-$PORT_DEFAULT}"

PROJ="${IEE_PROJECT_DIR:-}"
if [ -z "$PROJ" ]; then
  PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

URL="http://127.0.0.1:$PORT/"
PY="$PROJ/venv/bin/python"
LOG="$PROJ/server.log"

# 从 Finder 双击启动时 PATH 只有系统那几个目录，Homebrew 装的 python 不在里面
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

say() {
  if [ "$GUI" = 1 ]; then
    osascript -e "display notification \"$1\" with title \"图片炸开编辑器\"" >/dev/null 2>&1
  else
    echo "$1"
  fi
}

die() {
  if [ "$GUI" = 1 ]; then
    osascript -e "display dialog \"$1\" with title \"图片炸开编辑器启动失败\" buttons {\"好\"} default button 1 with icon stop" >/dev/null 2>&1
  else
    echo ""
    echo "启动失败：$1"
    echo ""
    echo "按回车键关闭窗口。"
    read -r _
  fi
  exit 1
}

alive() { curl -s -o /dev/null -m 2 "$URL"; }

# 程序装在「文稿」文件夹里，macOS 会为每个新的启动身份弹一次「想访问文稿文件夹」。
# 点了「允许」就一劳永逸；点了「不允许」或没理它，进程读不到目录只能干瞪眼 ——
# 原来这里是 `cd ... || exit 1`，双击后窗口一闪就没了，看不出是权限问题。
if [ ! -r "$PROJ/backend/server.py" ]; then
  die "读不到程序目录：

$PROJ

macOS 需要你授权访问「文稿」文件夹。要么在弹出的询问框里点「允许」，
要么打开 系统设置 → 隐私与安全性 → 文件与文件夹，给「图片炸开编辑器」
（或「终端」）勾上「文稿文件夹」，然后重新双击一次。"
fi
cd "$PROJ" || die "进不去程序目录：$PROJ"

IEE_PROJ="$PROJ"
# shellcheck source=mac_common.sh
. "$PROJ/scripts/mac_common.sh"

# 已经在跑就别起第二个，直接把浏览器切过去。反复双击图标是最常见的操作，
# 起一堆抢同一个端口的进程只会让人以为程序坏了。
if alive; then
  say "服务已在运行，正在打开浏览器…"
  open "$URL"
  exit 0
fi

# 端口上有东西但 HTTP 探不通：可能是自己上次留下的半死进程（收拾掉），
# 也可能是别的程序占了这个端口（换端口，绝不去杀别人的进程）。
if ! port_free "$PORT"; then
  if port_taken_by_others "$PORT"; then
    for try in $(seq $((PORT + 1)) $((PORT + 12))); do
      if port_free "$try"; then
        say "端口 $PORT 被别的程序占着，改用 $try"
        PORT="$try"
        URL="http://127.0.0.1:$PORT/"
        break
      fi
    done
  else
    stop_ours "$PORT" >/dev/null
  fi
fi

# 首次运行：建虚拟环境装依赖，约 1-3 分钟
if [ ! -x "$PY" ]; then
  say "首次启动，正在安装依赖，约 1-3 分钟，请稍候…"
  [ "$GUI" = 1 ] || echo "（这一步只在第一次运行时发生）"
  PY3=""
  for cand in python3.12 python3.13 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY3="$(command -v "$cand")"; break; fi
  done
  [ -n "$PY3" ] || die "这台电脑上找不到 python3，请先装 Python 3.11 以上版本。"
  "$PY3" -m venv "$PROJ/venv" >>"$LOG" 2>&1 || die "创建虚拟环境失败，详见 server.log"
  "$PROJ/venv/bin/pip" install -q --upgrade pip >>"$LOG" 2>&1
  "$PROJ/venv/bin/pip" install -q -r "$PROJ/requirements.txt" >>"$LOG" 2>&1 \
    || die "安装依赖失败，详见 $LOG"
  say "依赖安装完成。"
fi

say "正在启动…"

# 用 start_new_session 起一个独立会话，而不是 `nohup ... &`。
# nohup 只挡 SIGHUP，进程组还是启动者的：从 IDE 终端或脚本里启动时，
# 那个终端会话一结束，整组一起被收走，服务就跟着没了（实测就是这样莫名其妙断的）。
# setsid 之后它自己是一组，关终端、关启动窗口都影响不到它。
SRV_PID="$("$PY" - "$PROJ/backend/server.py" "$PORT" "$LOG" <<'PYEOF'
import subprocess, sys
script, port, log = sys.argv[1:4]
with open(log, "w") as fh:
    p = subprocess.Popen([sys.executable, script, "--port", port],
                         stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True)
print(p.pid)
PYEOF
)"
echo "$SRV_PID" > "$(pidfile_for "$PORT")"

# 冷启动要载 OCR 模型，给足 40 秒
for _ in $(seq 1 40); do
  alive && break
  # 进程自己挂了就别干等了
  kill -0 "$SRV_PID" 2>/dev/null || break
  sleep 1
done

if alive; then
  open "$URL"
  say "已启动，浏览器已打开 $URL"
  if [ "$GUI" != 1 ]; then
    echo ""
    echo "服务已在后台运行：$URL"
    echo "想停掉就双击「停止编辑器.command」，日志在 server.log。"
    echo ""
    echo "这个窗口可以关掉了（关窗口不会停掉服务）。"
  fi
  exit 0
fi

die "服务没能起来。日志最后几行：

$(tail -12 "$LOG" 2>/dev/null)"
