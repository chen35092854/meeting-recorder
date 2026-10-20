#!/bin/zsh
# 推送到 GitHub：读取 .env 中的 GITHUB_USERNAME / GITHUB_TOKEN
# 创建「私有仓库」meeting-recorder 并推送 main 分支 + v1.0.0 标签
set -e
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "缺少 .env，请先在项目根创建（可从归档快照复制）"
  exit 1
fi

USERNAME=$(grep -E '^GITHUB_USERNAME=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
TOKEN=$(grep -E '^GITHUB_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")

if [ -z "$USERNAME" ] || [ -z "$TOKEN" ]; then
  echo "请在 .env 中填写："
  echo "  GITHUB_USERNAME=<你的 GitHub 用户名>"
  echo "  GITHUB_TOKEN=<Personal Access Token，须勾选 repo 权限>"
  echo "（GitHub → Settings → Developer settings → Personal access tokens）"
  exit 1
fi

export GH_TOKEN="$TOKEN"
REPO="$USERNAME/meeting-recorder"

if gh repo view "$REPO" >/dev/null 2>&1; then
  echo "仓库已存在: $REPO（直接推送）"
  git remote add origin "https://github.com/$REPO.git" 2>/dev/null || git remote set-url origin "https://github.com/$REPO.git"
else
  echo "创建私有仓库并推送: $REPO"
  gh repo create "$REPO" --private --source . --push \
    --description "会议录音与 AI 纪要（本地实时转写 + 声纹分人 + 六模块纪要）"
fi

git push origin main --tags
echo ""
echo "✔ 已推送 https://github.com/$REPO"
