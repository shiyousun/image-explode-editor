#!/bin/bash
# 双击我就能停掉图片炸开编辑器
cd "$(dirname "$0")" || exit 1

IEE_PROJ="$PWD"
. "$IEE_PROJ/scripts/mac_common.sh"

PORT="${1:-8770}"
FOUND=1

# 主端口和启动时可能顺延到的备用端口一起扫
for p in $(seq "$PORT" $((PORT + 12))); do
  if stop_ours "$p"; then FOUND=0; fi
done

[ "$FOUND" = 0 ] || echo "服务本来就没在跑。"

echo ""
echo "按回车键关闭窗口。"
read -r _
