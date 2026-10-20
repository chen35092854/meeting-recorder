#!/bin/zsh
# 双击启动 WorkBuddy 会议录音（GUI）
cd "$(dirname "$0")"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
if [ ! -x ".venv/bin/python" ]; then
  echo "尚未安装依赖，请先运行: bash install.sh"
  read -k1 "?按任意键退出…"
  exit 1
fi
exec arch -arm64 ./.venv/bin/python run_recorder.py
