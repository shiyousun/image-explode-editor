#!/usr/bin/env bash
# 生成「图片炸开编辑器.app」—— 双击即用、可拖进 Dock / 应用程序的启动器
#
# 用法：bash scripts/build_mac_app.sh
# 换机器或改了目录位置后重跑一次即可（app 里写死了项目绝对路径）。
#
# 为什么绕 osacompile 而不是自己拼 .app：
#   .app 的主程序必须是 Mach-O 可执行文件。把 shell 脚本填进 CFBundleExecutable，
#   双击后 LaunchServices 直接静默什么都不做（open 还返回 0，非常难查）。
#   osacompile 生成的 applet 自带官方二进制壳，脚本挂在它后面跑，这是不用编译器就能拿到
#   合法 .app 的最省事办法。
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$PROJ/图片炸开编辑器.app"
ICON_SRC="${1:-$PROJ/assets/app_icon.png}"

cd "$PROJ"
rm -rf "$APP"

SRC="$(mktemp -t iee_applet).applescript"
cat > "$SRC" <<APPLESCRIPT
-- 图片炸开编辑器启动器：把活儿全交给 mac_launch.sh，自己立刻退出，
-- 免得 applet 在 Dock 上转圈。装依赖进度、失败提示都由那个脚本用通知和对话框呈现。
on run
	do shell script "IEE_PROJECT_DIR=" & quoted form of "$PROJ" & " nohup /bin/bash " & quoted form of "$PROJ/scripts/mac_launch.sh" & " --gui >/dev/null 2>&1 &"
end run
APPLESCRIPT

osacompile -o "$APP" "$SRC"
rm -f "$SRC"

# 图标：png → iconset → icns，替换 applet 自带的那个
if [ -f "$ICON_SRC" ]; then
  ICONSET="$(mktemp -d)/icon.iconset"
  mkdir -p "$ICONSET"
  for s in 16 32 64 128 256 512; do
    sips -z $s $s "$ICON_SRC" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$ICON_SRC" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/applet.icns"
  rm -rf "$(dirname "$ICONSET")"
fi

PL="$APP/Contents/Info.plist"
set_key() { /usr/libexec/PlistBuddy -c "Set :$1 $2" "$PL" 2>/dev/null || /usr/libexec/PlistBuddy -c "Add :$1 string $2" "$PL"; }
set_key CFBundleName "图片炸开编辑器"
set_key CFBundleDisplayName "图片炸开编辑器"
set_key CFBundleIdentifier "ai.friendsun.imageexplodeeditor"
set_key CFBundleShortVersionString "1.1"
set_key CFBundleVersion "1.1"
/usr/libexec/PlistBuddy -c "Delete :CFBundleGetInfoString" "$PL" 2>/dev/null || true

# 不签名的 app 在新系统上会被挡；ad-hoc 签一下就够本机自用
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true

touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" 2>/dev/null || true

echo "已生成：$APP"
echo "双击即可启动；也可以拖到 Dock 或「应用程序」文件夹。"
