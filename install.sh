#!/bin/zsh
# 会议录音依赖安装脚本（首次运行执行一次）
set -e
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "未找到 python3，请先安装 Python 3.10+"
  exit 1
fi
echo "使用 Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi
./.venv/bin/python -m pip install -U pip -q
./.venv/bin/pip install -r requirements.txt
mkdir -p recordings logs

echo ""
echo "✔ 依赖安装完成"
echo "首次运行会自动下载语音识别模型 small（约 460MB，默认走国内镜像 hf-mirror.com，"
echo "  如需官方源：export HF_ENDPOINT=https://huggingface.co 后重新运行）"
echo "启动方式：双击 launch.command，或在 WorkBuddy 对话输入 /meeting-recorder"
