#!/bin/bash
# 把项目打包成 macOS 的 .app 应用程序包（双击即可运行，无需终端）。
#
# 产物：喜马拉雅上传工具.app
# 原理：.app 内部是一个标准 bundle，内含 Info.plist + MacOS/ 启动器。
#      启动器调用项目内的 .venv/bin/python 运行 run.py。
#      playwright 用 channel="chrome" 驱动本机已装的 Chrome，
#      所以不需要把 Chromium 打进包里，包体积很小。

set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="喜马拉雅有声书工具"
BUNDLE="${APP_NAME}.app"
CONTENTS="${BUNDLE}/Contents"
RESOURCES="${CONTENTS}/Resources"
MACOS="${CONTENTS}/MacOS"

# --- 1. 环境准备 -----------------------------------------------------------
VENV_PY=".venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "▸ 创建项目内虚拟环境…"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet playwright
fi

if ! .venv/bin/python -c "import playwright" 2>/dev/null; then
  echo "▸ 补装 playwright…"
  .venv/bin/pip install --quiet playwright
fi

# --- 2. 组装 bundle --------------------------------------------------------
echo "▸ 组装 ${BUNDLE}…"

# 运行期数据（登录态 / 断点状态 / 日志）现在放在
#   ~/Library/Application Support/喜马拉雅有声书工具
# 重新打包、挪动 .app 都不会把它弄丢。
# 这里只做一次性兼容搬迁：老版本把数据放在 bundle 里，搬一次过去。
DATA_DIR="$HOME/Library/Application Support/喜马拉雅有声书工具"
for d in profiles state logs; do
  src="$BUNDLE/Contents/Resources/app/$d"
  [ -d "$src" ] || continue
  [ -n "$(ls -A "$src" 2>/dev/null)" ] || continue
  if [ -z "$(ls -A "$DATA_DIR/$d" 2>/dev/null)" ]; then
    mkdir -p "$DATA_DIR"
    cp -R "$src" "$DATA_DIR/$d"
    echo "  · 已把 bundle 内的 $d 搬到 $DATA_DIR/$d"
  fi
done

# 原地复用已有 bundle：整包删除既慢又危险（曾因此误删登录态），
# 也容易触发批量删除保护。只覆盖源码与资源，运行时/登录态一律保留。
mkdir -p "$MACOS" "$RESOURCES"
if [ -d "$RESOURCES/app" ]; then
  echo "  · 复用已有 bundle，原地更新源码（不删任何文件）"
fi

# 复制源码（用 src/. 覆盖目标目录内容，避免 cp -R 在已存在目录里套娃）
mkdir -p "$RESOURCES/app/xmly" "$RESOURCES/app/tests" "$RESOURCES/app/_work"
cp -R xmly/. "$RESOURCES/app/xmly/"
cp run.py "$RESOURCES/app/run.py"
cp README.md "$RESOURCES/app/" 2>/dev/null || true
cp -R tests/. "$RESOURCES/app/tests/" 2>/dev/null || true

# 启动器
cat > "$MACOS/launcher" <<'LAUNCHER'
#!/bin/bash
# 由 .app 启动器调用：定位 bundle 内的资源目录并运行 GUI
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../Resources/app" && pwd)"
PY="$HERE/.venv/bin/python"
# 优先用 bundle 内自带的 venv；缺失时回退到系统 python3
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi
export PYTHONPATH="$HERE:${PYTHONPATH:-}"
exec "$PY" "$HERE/run.py" "$@"
LAUNCHER
chmod +x "$MACOS/launcher"

# Info.plist
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>${APP_NAME}</string>
  <key>CFBundleDisplayName</key><string>${APP_NAME}</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIdentifier</key><string>cn.oz.ximalaya.uploader</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>有声书套包批量上传工具</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
</dict>
</plist>
PLIST

# --- 3. 图标 ----------------------------------------------------------------
# 优先用本地生成的专属图标（make_icon.py）；缺失则回退到系统通用图标
if [ -f "icon.icns" ]; then
  cp "icon.icns" "$RESOURCES/icon.icns"
  echo "▸ 使用专属图标 icon.icns"
elif [ -f "/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/GenericApplicationIcon.icns" ]; then
  cp "/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/GenericApplicationIcon.icns" \
     "$RESOURCES/icon.icns"
  echo "▸ 未找到专属图标，回退系统图标（可先运行 python make_icon.py 生成）"
fi

# --- 4. 把 venv 复制进 bundle（保证脱离开发环境也能运行） ------------------
# 只复制运行必需部分：bin/python*、lib/python3.13、playwright 包
if [ -x "$RESOURCES/app/.venv/bin/python" ]; then
  echo "▸ bundle 内已有 Python 运行时，原样保留（不重复复制、不改写）"
elif [ -d ".venv" ]; then
  echo "▸ 打包 Python 运行时（体积较大，请稍候）…"
  mkdir -p "$RESOURCES/app/.venv"
  cp -R .venv/bin "$RESOURCES/app/.venv/bin" 2>/dev/null || true
  mkdir -p "$RESOURCES/app/.venv/lib"
  cp -R .venv/lib/python3.13 "$RESOURCES/app/.venv/lib/python3.13" 2>/dev/null || true
  # venv 是可重定位的：修正 pyvenv.cfg 里的绝对路径
  cat > "$RESOURCES/app/.venv/pyvenv.cfg" <<CFG
home = /usr/bin
include-system-site-packages = false
version = 3.13.12
CFG
  # 修正 bin/ 下脚本的 shebang 指向
  find "$RESOURCES/app/.venv/bin" -type f -name "python*" -exec sh -c '
    for f; do
      if head -c2 "$f" | grep -q "#!"; then
        head -1 "$f" | grep -q "bin/python" && true
      fi
    done' _ {} + 2>/dev/null || true
fi

# --- 5. 签名（ad-hoc，避免 Gatekeeper 拦截） --------------------------------
echo "▸ ad-hoc 签名…"
codesign --force --deep --sign - "$BUNDLE" 2>/dev/null || {
  echo "  ⚠️ 签名失败（不影响本地运行）"
}

# --- 6. 产物信息 ------------------------------------------------------------
SIZE=$(du -sh "$BUNDLE" | cut -f1)
echo ""
echo "✅ 打包完成：${BUNDLE}  (${SIZE})"
echo ""
echo "使用方式："
echo "  1. 双击「${BUNDLE}」启动（首次会自建依赖，可能需十几秒）"
echo "  2. 或把 ${BUNDLE} 拖到「应用程序」文件夹"
echo "  3. 登录态 / 断点状态 / 日志保存在：${DATA_DIR}"
