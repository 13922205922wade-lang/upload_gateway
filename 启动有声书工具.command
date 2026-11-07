#!/bin/bash
# 双击启动喜马拉雅有声书上传工具。
# 首次运行会自动建虚拟环境并装依赖。

cd "$(dirname "$0")" || exit 1

VENV=".venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "首次运行：正在创建虚拟环境并安装依赖…"
  python3 -m venv "$VENV" || { echo "创建虚拟环境失败"; read -r -p "按回车关闭…"; exit 1; }
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet playwright || {
    echo "依赖安装失败"; read -r -p "按回车关闭…"; exit 1; }
fi

# 确认 playwright 的 Python 包可用
"$PY" -c "import playwright" 2>/dev/null || {
  echo "补装 playwright…"; "$VENV/bin/pip" install --quiet playwright; }

exec "$PY" run.py "$@"
