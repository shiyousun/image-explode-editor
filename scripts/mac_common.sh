#!/bin/bash
# 启动/停止脚本共用的进程识别逻辑。
#
# PID 文件按端口分开记：.server.<端口>.pid。
# 早先只有一个 .server.pid，端口冲突顺延到别的端口再起一个实例时，它会把前一个的记录冲掉，
# 于是 8770 上那个变成没人认领的孤儿（端口扫描还能兜住，但 PID 文件已经在骗人了）。
pidfile_for() { echo "$IEE_PROJ/.server.$1.pid"; }

# 认「是不是我们自己的服务」不能只靠 PID 文件（手动 python backend/server.py 起的没有这个文件），
# 也不能只靠命令行里匹配路径（相对路径起的是 "backend/server.py"，绝对路径匹配不到）。
# 这里的判据是两条同时成立：进程的工作目录就是本项目，且命令行跑的是 backend/server.py。
# 这样既不会漏掉自己起的进程，也绝不会误杀别人占着同一个端口的程序。

# 只列真正「监听」这个端口的进程。
#
# 不能用 `lsof -ti tcp:8770`：它把出站连接也算进去。实测本机连过远程服务器的 8770 之后，
# 浏览器进程就一直挂着几条到 21.91.41.143:8770 的 CLOSED 记录，于是启动器以为本机端口被占，
# 莫名其妙换到 8771，而用户还在按 8770 找页面。加 -sTCP:LISTEN 就只看真正在听的。
listeners_on() {
  lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null
}

proc_is_ours() {
  local pid="$1" cwd
  [ -n "$pid" ] || return 1
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
  [ "$cwd" = "$IEE_PROJ" ] || return 1
  ps -o command= -p "$pid" 2>/dev/null | grep -q "backend/server.py"
}

# 停掉本项目在指定端口上的服务，返回 0 表示确实停掉了东西
stop_ours() {
  local port="$1" stopped=1 pid killed=" "
  for pid in $(listeners_on "$port") $(cat "$(pidfile_for "$port")" 2>/dev/null); do
    # 监听端口的进程和 PID 文件里记的往往是同一个，别重复汇报
    case "$killed" in *" $pid "*) continue ;; esac
    if kill -0 "$pid" 2>/dev/null && proc_is_ours "$pid"; then
      kill "$pid" 2>/dev/null
      killed="$killed$pid "
      stopped=0
      echo "已停止服务（PID $pid）"
    fi
  done
  [ "$stopped" = 0 ] && rm -f "$(pidfile_for "$port")" && sleep 1
  return $stopped
}

# 端口上是否有别人的程序（不是我们的服务）
port_taken_by_others() {
  local port="$1" pid
  for pid in $(listeners_on "$port"); do
    proc_is_ours "$pid" || return 0
  done
  return 1
}

port_free() {
  [ -z "$(listeners_on "$1")" ]
}
