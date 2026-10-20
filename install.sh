#!/bin/zsh
# ============================================================
#  会议录音 · 一键启动（唯一入口）
#  双击本文件：自动检查环境 → 缺依赖自动安装 → 启动录音界面
#  兼容：终端执行 bash install.sh 亦可（未安装依赖会自动装）
# ============================================================
set -e

cd "$(dirname "$0")"

# 出错兜底：窗口不闪退，提示截图留证
err() {
  code=$?
  echo ""
  echo "✖ 步骤出错（错误码 $code），请截图本窗口发给测试负责人。"
  echo "按回车关闭窗口…"
  read -r dummy || true
  exit 1
}
trap err ERR

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
VENV_PY=".venv/bin/python"

echo "══════════════════════════════════════"
echo "  会议录音助手 · 一键启动"
echo "══════════════════════════════════════"

# ---------- 1. 平台 ----------
case "$(uname -s)" in
  Darwin) REQ_FILE="requirements-mac.txt" ;;
  Linux)  REQ_FILE="requirements-win.txt" ;;
  *)      echo "暂不支持该系统: $(uname -s)（目前支持 macOS / Linux）"
          echo "按回车关闭窗口…"
          read -r dummy || true
          exit 1 ;;
esac

# ---------- 2. 检测 Python（要求 3.10+） ----------
PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ] || ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "未找到 python3。"
  echo "请先安装 Python 3.10+ 后重新双击本文件："
  echo "  下载地址 https://www.python.org/downloads/（安装时勾选 Add Python to PATH）"
  echo "按回车关闭窗口…"
  read -r dummy || true
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
  echo "检测到 Python：$("$PYTHON_BIN" --version 2>&1)，需要 3.10 及以上版本。"
  echo "请升级后重新双击本文件：https://www.python.org/downloads/"
  echo "按回车关闭窗口…"
  read -r dummy || true
  exit 1
fi
echo "√ 检测到 Python: $("$PYTHON_BIN" --version 2>&1)"

# ---------- 3. 依赖检查 / 自动安装 ----------
NEED_INSTALL=1
if [ -x "$VENV_PY" ]; then
  if "$VENV_PY" -c "import faster_whisper" >/dev/null 2>&1; then
    NEED_INSTALL=0
  fi
fi

if [ "$NEED_INSTALL" = "0" ]; then
  echo "√ 依赖已就绪，直接启动…"
else
  echo "首次使用：正在创建环境并安装依赖（约 2~5 分钟，请勿关闭窗口）…"
  if [ ! -d ".venv" ]; then
    "$PYTHON_BIN" -m venv .venv
  fi

  # 代理检测：终端常驻代理变量但代理软件未启动 → pip 全部走死代理
  # （ProxyError / Connection refused）。先提示，失败自动忽略代理+镜像重试。
  PROXY_HINT="$(env | grep -E '^(http|https|all)_proxy=' | head -2 || true)"
  if [ -n "$PROXY_HINT" ]; then
    echo "  检测到代理环境变量："
    echo "$PROXY_HINT" | sed 's/^/    /'
    echo "  （若代理未开启导致下载失败，脚本会自动忽略代理并用国内镜像重试）"
  fi

  # pip 本身可跳过升级（不致命）
  "$VENV_PY" -m pip install -U pip -q --retries 2 --timeout 20 \
    || echo "  （pip 升级跳过，继续安装依赖）"

  if "$VENV_PY" -m pip install --retries 2 --timeout 20 -r "$REQ_FILE"; then
    true
  else
    echo ""
    echo "  第一次安装失败，自动改用「忽略代理 + 清华 PyPI 镜像」重试…"
    env -u http_proxy -u https_proxy -u all_proxy \
        -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
        "$VENV_PY" -m pip install --retries 2 --timeout 20 \
        -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$REQ_FILE"
  fi
  mkdir -p recordings logs
  echo "√ 依赖安装完成"
fi

# ---------- 4. 启动录音界面 ----------
echo ""
echo "正在启动会议录音界面…"
echo "（首次启动会自动下载语音识别模型，约数百 MB，默认走国内镜像 hf-mirror.com，请耐心等待）"
echo "若界面未弹出，请把本窗口内容截图发给测试负责人。"
echo ""
"$VENV_PY" run_recorder.py
echo ""
echo "程序已退出，按回车关闭窗口。"
read -r dummy || true
