#!/bin/bash
# 沙盒测试 install.sh 2.5 段 v1.0.10「版本降级自动安装 Tk」逻辑
# 模拟同事真实场景:Homebrew python@3.11 缺 Tk,且 python-tk@3.11 已 EOL 无 bottle
# 预期:自动依次尝试 3.11(失败)→ 3.13(成功)→ 切换 PYTHON_BIN 到 3.13 继续流程
#
# 场景:
#   FALLBACK  : 3.11 无 bottle + 3.13 有 bottle → 自动切换 3.13 继续(核心回归)
#   ALL_FAIL  : 全部版本失败 → 提示「自动尝试 4 个版本均未成功」+ 错误详情,无手动命令
#   FIRST_OK  : 当前版本直接成功(3.13 用户) → 不降级直接继续
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX="$ROOT/scripts/.tk_sandbox/homebrew"
rm -rf "$(dirname "$SANDBOX")"
mkdir -p "$SANDBOX/binworking" "$SANDBOX/stage" "$SANDBOX/opt"

# ---------- 伪 python3(3.11,符合 install.sh 检测路径,缺 Tk) ----------
# 注意:$SANDBOX 是写入期常量(展开);\$* 等是运行期变量(转义)
cat > "$SANDBOX/binworking/python3" <<EOF
#!/bin/zsh
args="\$*"
case "\$args" in
  *"import tkinter"*) [ -f "$SANDBOX/tk_ok" ] && exit 0 || exit 1 ;;
  *"sys.version_info >= (3,10)"*) exit 0 ;;
  *"print(f\"{v.major}.{v.minor}\")"*) echo "3.11" ;;
  *"--version"*) echo "Python 3.11.15" ;;
  *"-m venv"*) mkdir -p .venv/bin; touch .venv/bin/python; chmod +x .venv/bin/python; exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$SANDBOX/binworking/python3"

# ---------- 伪 python3.13(降级后 install.sh 探测到,自带 Tk) ----------
mkdir -p "$SANDBOX/opt/python@3.13/libexec/bin"
cat > "$SANDBOX/opt/python@3.13/libexec/bin/python3.13" <<EOF
#!/bin/zsh
args="\$*"
case "\$args" in
  *"import tkinter"*) exit 0 ;;   # 3.13 自带 Tk(降级成功标志)
  *"sys.version_info >= (3,10)"*) exit 0 ;;
  *"print(f\"{v.major}.{v.minor}\")"*) echo "3.13" ;;
  *"--version"*) echo "Python 3.13.16" ;;
  *"-m venv"*) mkdir -p .venv/bin; touch .venv/bin/python; chmod +x .venv/bin/python; exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$SANDBOX/opt/python@3.13/libexec/bin/python3.13"

# ---------- 伪 brew:FAIL_VERSIONS 环境变量(运行期)决定哪些版本失败 ----------
cat > "$SANDBOX/binworking/brew" <<EOF
#!/bin/zsh
if [ "\$1" = "--prefix" ]; then
  if [ "\$2" = "python@3.13" ]; then echo "$SANDBOX/opt/python@3.13"; fi
  exit 0
fi
ver="\${2#python-tk@}"   # 提取纯版本号:python-tk@3.13 → 3.13
case "\${FAIL_VERSIONS:-}" in
  *"\$ver"*) echo "Error: python-tk@\$ver: no bottle available!" >&2; exit 1 ;;
esac
echo "==> Pouring python-tk@\$ver (fake)"
exit 0
EOF
chmod +x "$SANDBOX/binworking/brew"

cp "$ROOT/install.sh" "$SANDBOX/stage/install.sh"
: > "$SANDBOX/stage/requirements-mac.txt"
: > "$SANDBOX/stage/requirements-win.txt"

export PATH="$SANDBOX/binworking:$PATH"
export PYTHON="$SANDBOX/binworking/python3"

echo "════════ 场景1:3.11 无 bottle,自动降级 3.13(同事真实场景) ════════"
FAIL_VERSIONS="3.11 3.12 3.14" bash "$SANDBOX/stage/install.sh" < /dev/null 2>&1 \
  | sed -n '/_tkinter/,$p' | head -22
echo ""
echo "════════ 场景2:全部版本失败 ════════"
FAIL_VERSIONS="3.11 3.13 3.12 3.14" bash "$SANDBOX/stage/install.sh" < /dev/null 2>&1 \
  | grep -E "均未成功|错误详情|手动执行" | head -4
echo ""
echo "════════ 场景3:当前版本直接成功(3.13 用户) ════════"
PYTHON="$SANDBOX/opt/python@3.13/libexec/bin/python3.13" \
  bash "$SANDBOX/stage/install.sh" < /dev/null 2>&1 \
  | grep -E "Tk 图形库|依赖已就绪" | head -4
echo ""
echo "== 沙盒测试结束 =="
