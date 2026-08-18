#!/bin/bash
# 双击我就能启动图片炸开编辑器（会开一个终端窗口，装依赖进度和报错都看得见）
cd "$(dirname "$0")" || exit 1
exec bash scripts/mac_launch.sh "$@"
