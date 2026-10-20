#!/bin/bash
# 沙盒测试 install.sh 2.5 段「自动安装 Tk」分支(不污染真实环境、不执行真实 brew/pip)
# v1.0.9 逻辑:检测到缺 Tk → 自动 brew install → 失败自动重试一次 → 均失败则引导重开
# 关键:wrapper 路径含 "homebrew" 字样,命中 install.sh 的识别分支(与同事真实场景一致)
#
# 场景:
#   FAIL_BREW    : brew 两次都失败 → 提示「自动安装两次均未成功」，无手动命令
#   RETRY_ONCE   : 第一次失败第二次成功 → 重试成功，继续流程
#   SUCCESS_FIRST: 第一次就成功 → 直接继续流程
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX="$ROOT/scripts/.tk_sandbox/homebrew"
rm -rf "$(dirname "$SANDBOX")"
mkdir -p "$SANDBOX/binworking" "$SANDBOX/stage"

# ---- 假 python3:按参数模拟「缺 tk / 已装 tk / 版本 / 版本号 / venv」----
cat > "$SANDBOX/binworking/python3" <<'EOF'
#!/bin/zsh
args="$*"
case "$args" in
  *"import tkinter"*) [ -f "$SANDBOX_TK_OK" ] && exit 0 || exit 1 ;;
  *"sys.version_info >= (3,10)"*) exit 0 ;;
  *"print(f\"{v.major}.{v.minor}\")"*) echo "3.11" ;;
  *"--version"*) echo "Python 3.11.15" ;;
  *"-m venv"*) mkdir -p .venv/bin; touch .venv/bin/python; chmod +x .venv/bin/python; exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$SANDBOX/binworking/python3"

# ---- 假 brew:用余额文件 FAKE_LIMIT 记录剩余失败次数(0 或空 = 全成功) ----
cat > "$SANDBOX/binworking/brew" <<'EOF'
#!/bin/zsh
LIMIT="${SANDBOX_BREW_FAIL_LIMIT:-0}"   # 剩余失败次数(文件内容)
if [ -n "$LIMIT" ] && [ "$LIMIT" -gt 0 ]; then
  echo "Error: brew fail (剩余失败 $LIMIT 次)" >&2
  echo "$((LIMIT - 1))" > "$SANDBOX_BREW_FAIL_FILE"
  exit 1
fi
echo "==> Pouring python-tk@3.11 (fake)"
touch "$SANDBOX_TK_OK"
exit 0
EOF
chmod +x "$SANDBOX/binworking/brew"

cp "$ROOT/install.sh" "$SANDBOX/stage/install.sh"
: > "$SANDBOX/stage/requirements-mac.txt"
: > "$SANDBOX/stage/requirements-win.txt"

export PATH="$SANDBOX/binworking:$PATH"
export PYTHON="$SANDBOX/binworking/python3"
export SANDBOX_TK_OK="$SANDBOX/tk_ok"
export SANDBOX_BREW_FAIL_FILE="$SANDBOX/fail_limit"

echo "════════ 场景1:brew 两次都失败(限额 99) ════════"
printf '99' > "$SANDBOX/fail_limit"; rm -f "$SANDBOX/tk_ok"
bash "$SANDBOX/stage/install.sh" < /dev/null 2>&1 \
  | grep -A6 "第 1 次尝试\|两次均未成功" | head -12
echo "  手动命令数:$(rm -f "$SANDBOX/tk_ok"; printf '99' > "$SANDBOX/fail_limit"; bash "$SANDBOX/stage/install.sh" < /dev/null 2>&1 | grep -c "手动执行")"

echo ""
echo "════════ 场景2:第一次失败第二次成功(限额 1) ════════"
printf '1' > "$SANDBOX/fail_limit"; rm -f "$SANDBOX/tk_ok"
bash "$SANDBOX/stage/install.sh" < /dev/null 2>&1 \
  | grep -E "第 1 次尝试|自动重试|安装成功|继续" | head -6

echo ""
echo "════════ 场景3:第一次就成功(限额 0) ════════"
printf '0' > "$SANDBOX/fail_limit"; rm -f "$SANDBOX/tk_ok"
bash "$SANDBOX/stage/install.sh" < /dev/null 2>&1 \
  | grep -E "第 1 次尝试|安装成功|继续|依赖" | head -6

echo ""
echo "== 沙盒测试结束 =="
