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

# 2026-10-21 关键修复：huggingface_hub 新版默认走 Xet 存储后端下载大文件，
# Xet 的 CAS 服务器(cas-server.xethub.hf.co)不在 hf-mirror.com 镜像覆盖范围内，
# 镜像 + Xet 必然 401 Unauthorized（同事 v1.0.10 实测：ASR 预热失败 → 转写空白）。
# HF_HUB_DISABLE_XET=1 强制回退普通 HTTP 下载，镜像随即生效。
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export HF_HUB_DISABLE_XET=1
export HF_HUB_DISABLE_PROGRESS_BARS=1
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

# ---------- 2.5 检测/自动补装 Tk 图形库（GUI 依赖） ----------
# 必须在创建 venv 之前用「系统 Python」检测：venv 继承系统 Python 的
# _tkinter 能力，Homebrew 的 python 默认不带 Tk（需额外 python-tk 包），
# 若等到启动时才 import 失败，用户会白等几分钟依赖安装（2026-10-20 实测）。
# v1.0.10：全自动 + 版本降级——老版本(如 3.11，已 EOL)的 python-tk 无 bottle，
# 自动改用受支持的 python-tk@3.13/3.12/3.14（连带装新 Python），全程无手动命令；
# 并用 yes 管道应答 brew 的 [y/n] 交互确认（同事实测 no bottle 教训）。
if ! "$PYTHON_BIN" -c "import tkinter" >/dev/null 2>&1; then
  PY_SHORT="$("$PYTHON_BIN" -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}")' 2>/dev/null || true)"
  echo ""
  echo "✖ 当前 Python 缺少 Tk 图形库（_tkinter），录音界面依赖它，无法启动。"
  echo "  检测到 Python: $("$PYTHON_BIN" --version 2>&1)"
  echo "  路径：$PYTHON_BIN"
  echo ""
  if echo "$PYTHON_BIN" | grep -q "homebrew" && [ -n "$PY_SHORT" ]; then
    echo "  你是用 Homebrew 安装的 Python——现在自动安装配套的 Tk 支持包"
    echo "  （约几十 MB，请耐心等待，过程中不要关闭窗口）…"
    echo ""
    if command -v brew >/dev/null 2>&1; then
      TK_LOG="$(mktemp -t tk_install)"
      TK_ANS="$(mktemp -t tk_ans)"
      echo "y" > "$TK_ANS"  # 预写 y：应答 brew 的 [y/n] 交互确认（避免卡住/放弃）
      # 尝试给指定版本安装 python-tk；成功且对应 Python 可 import 返回 0
      try_tk_ver() {
        ver="$1"
        echo "  → 尝试安装 python-tk@$ver …"
        # 用 stdin 重定向喂 y（不用管道，避免 zsh/bash 的 PIPESTATUS 差异）
        HOMEBREW_NO_AUTO_UPDATE=1 brew install "python-tk@$ver" \
          < "$TK_ANS" >>"$TK_LOG" 2>&1 || return 1
        # 优先探测该版本对应的 Python（新装时会连带 python@$ver）
        cand="$(brew --prefix "python@$ver" 2>/dev/null)/libexec/bin/python$ver"
        if [ -x "$cand" ] && "$cand" -c "import tkinter" >/dev/null 2>&1; then
          PYTHON_BIN="$cand"
          PY_SHORT="$ver"
          return 0
        fi
        # 同版本场景：原 Python 直接用
        if "$PYTHON_BIN" -c "import tkinter" >/dev/null 2>&1; then
          return 0
        fi
        return 1
      }
      TK_INSTALLED=0
      for v in "$PY_SHORT" 3.13 3.12 3.14; do
        if [ "$v" = "$PY_SHORT" ]; then
          msg="你的版本 python-tk@$v"
        else
          msg="你的版本已停止支持，自动改用受支持的 python-tk@$v"
        fi
        echo "  （$msg）"
        if try_tk_ver "$v"; then
          TK_INSTALLED=1
          break
        fi
      done
      if [ "$TK_INSTALLED" = "1" ]; then
        # 旧 .venv 可能是缺 Tk 时建的残缺环境 / 旧 Python 版本，删掉让后续流程重建
        if [ -d ".venv" ]; then
          rm -rf .venv
          echo "  （已清理旧的 .venv，将自动重建）"
        fi
        echo "√ Tk 图形库安装成功，继续…"
      else
        echo ""
        echo "✖ 自动尝试 4 个版本均未成功（多为网络或 brew 源问题）。"
        echo "  错误详情（供排查）："
        tail -6 "$TK_LOG" 2>/dev/null | sed 's/^/    /'
        echo ""
        echo "  请检查网络连接（或关闭代理软件）后，关闭本窗口重新双击本文件，"
        echo "  会自动重试；多次失败请把本窗口截图发给测试负责人。"
        rm -f "$TK_LOG" "$TK_ANS"
        echo ""
        echo "按回车关闭窗口…"
        read -r dummy || true
        exit 1
      fi
      rm -f "$TK_LOG" "$TK_ANS"
    else
      echo "  未检测到 brew 命令（Homebrew 缺失），无法自动安装。"
      echo "  请把本窗口截图发给测试负责人协助处理。"
      echo ""
      echo "按回车关闭窗口…"
      read -r dummy || true
      exit 1
    fi
  else
    echo "  请改用 python.org 官方 Python（自带 Tk 支持）："
    echo "      https://www.python.org/downloads/"
    echo ""
    echo "按回车关闭窗口…"
    read -r dummy || true
    exit 1
  fi
fi
echo "√ Tk 图形库就绪"

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
echo "（首次使用会在向导第 1 步自动下载语音识别模型，界面内实时显示进度；"
echo "  下载连续失败时会给出直链，可人工下载后导入）"
echo "若界面未弹出，请把本窗口内容截图发给测试负责人。"
echo ""
"$VENV_PY" run_recorder.py
echo ""
echo "程序已退出，按回车关闭窗口。"
read -r dummy || true
