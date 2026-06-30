#!/bin/zsh
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

CODEX_PY="/Users/Zhuanz/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

if [ -x "$CODEX_PY" ]; then
  PYTHON="$CODEX_PY"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "未找到 Python 3。请先安装 Python 3。"
  read -r "?按回车退出..."
  exit 1
fi

echo "使用 Python：$PYTHON"

if ! "$PYTHON" - <<'PY'
import tkinter
from PIL import Image
PY
then
  echo ""
  echo "缺少运行依赖 tkinter 或 Pillow。"
  echo "请在终端执行："
  echo "  $PYTHON -m pip install Pillow"
  echo ""
  read -r "?按回车退出..."
  exit 1
fi

"$PYTHON" "$APP_DIR/archive_image_tool.py"
