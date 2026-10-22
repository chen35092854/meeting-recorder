#!/bin/zsh
# 推送到 GitHub：读取 .env 中的 GITHUB_USERNAME / GITHUB_TOKEN
# 目标：私有仓库 meeting-recorder，推送 main 分支 + 所有本地标签（如 v1.0.0）
#
# 设计要点：
#  1. 变量名用 GH_USER / GH_TOKEN，避开 zsh 内置特殊变量 USERNAME（#!zsh 下 USERNAME 恒等于登录名，赋值无效）
#  2. git push 用「带 Token 的 URL」，不依赖系统凭据；推送后立即还原 remote（token 不残留在配置里）
#  3. 若 github.com 隧道不通（常见于代理环境，报 CONNECT tunnel failed 502），
#     自动降级走 GitHub REST API（api.github.com 通常可达）同步分支/标签 refs
set -e
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "缺少 .env，请先在项目根创建（可从归档快照复制）"
  exit 1
fi

GH_USER=$(grep -E '^GITHUB_USERNAME=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')
GH_TOKEN=$(grep -E '^GITHUB_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')

if [ -z "$GH_USER" ] || [ -z "$GH_TOKEN" ]; then
  echo "请在 .env 中填写："
  echo "  GITHUB_USERNAME=<你的 GitHub 用户名>"
  echo "  GITHUB_TOKEN=<Personal Access Token，须勾选 repo 权限>"
  echo "（GitHub → Settings → Developer settings → Personal access tokens）"
  exit 1
fi

export GH_TOKEN="$GH_TOKEN"
REPO="$GH_USER/meeting-recorder"
GIT_PUSH_URL="https://${GH_USER}:${GH_TOKEN}@github.com/${REPO}.git"

# ---------- 1. 确保远程仓库存在 ----------
if gh repo view "$REPO" >/dev/null 2>&1; then
  echo "仓库已存在: $REPO（直接推送）"
  git remote add origin "https://github.com/$REPO.git" 2>/dev/null || git remote set-url origin "https://github.com/$REPO.git"
else
  echo "创建私有仓库并推送: $REPO"
  gh repo create "$REPO" --private --source . --push \
    --description "会议录音与 AI 纪要（本地实时转写 + 声纹分人 + 六模块纪要）"
fi

# ---------- 2. 标准 git push ----------
echo ""
echo ">>> [1/2] git push（分支 + 标签）..."
if git push "$GIT_PUSH_URL" main --tags 2>&1; then
  : # 成功
elif git push "$GIT_PUSH_URL" --tags 2>&1 && git push "$GIT_PUSH_URL" main 2>&1; then
  : # 分支名不是 main 时的兜底
else
  echo ""
  echo ">>> [2/2] git push 失败（多半是 github.com 隧道不通），改用 REST API 同步 refs ..."
  /Users/eric/.workbuddy/binaries/python/versions/3.13.12/bin/python3 - "$GH_TOKEN" "$REPO" <<'PY'
import json
import subprocess
import sys
import urllib.request

token, repo = sys.argv[1], sys.argv[2]
API = f"https://api.github.com/repos/{repo}"


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def local_refs():
    """返回 {ref: sha}，如 refs/heads/main / refs/tags/v1.0.0"""
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags"],
        capture_output=True, text=True, check=True,
    ).stdout
    return {line.split()[1]: line.split()[0] for line in out.splitlines() if line.strip()}


def remote_exists(obj_sha):
    try:
        api(f"/git/commits/{obj_sha}")
        return True
    except Exception:
        return False


def remote_refs():
    try:
        return {r["ref"]: r["object"]["sha"] for r in api("/git/refs")}
    except Exception:
        return {}


ok, fail = True, []
remote = remote_refs()
for ref, sha in sorted(local_refs().items()):
    short = ref.replace("refs/", "")
    rsha = remote.get(ref)
    if rsha == sha:
        print(f"  已同步  {short} -> {sha[:7]}（远程一致）")
    elif rsha and rsha != sha:
        if not remote_exists(sha):
            fail.append(f"{short}: 远程已有 {rsha[:7]}，本地 {sha[:7]} 的对象尚未在远程，请网络恢复后重跑 git push")
            continue
        api(f"/git/refs/{short}", method="PATCH", body={"sha": sha, "force": True})
        print(f"  已更新  {short} -> {sha[:7]}（远程原为 {rsha[:7]}）")
    else:
        if not remote_exists(sha):
            fail.append(f"{short}: 本地对象 {sha[:7]} 尚未在远程，请网络恢复后重跑 git push")
            continue
        api("/git/refs", method="POST", body={"ref": ref, "sha": sha})
        print(f"  已创建  {short} -> {sha[:7]}")

if fail:
    print("\n以下 refs 未同步（原因见上）：")
    for f in fail:
        print("  ⚠ " + f)
    sys.exit(2)
print("\n✔ REST API 兜底同步完成")
PY
  SYNC_RC=$?
  if [ "$SYNC_RC" -ne 0 ]; then
    echo ""
    echo ">>> REST refs 同步不足（main 的新 commit 对象无法经 REST 上传），"
    echo ">>> 改走 Git Data API 全链同步（重建 blob→tree→commit→ref）..."
    PYV=$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
    if "$PYV" scripts/gh_data_sync.py; then
      echo "✔ Git Data API 同步完成（远程为重建 commit，内容与本地一致）"
      echo "  提示：网络恢复后重跑本脚本一次，可让两边 commit sha 完全对齐"
    else
      echo "⚠ Git Data API 同步失败（网络仍受阻），恢复网络后重跑：bash scripts/push_github.sh"
    fi
  fi
fi

# ---------- 3. 还原 remote（去掉凭据） ----------
git remote set-url origin "https://github.com/$REPO.git" 2>/dev/null
echo ""
echo "✔ 完成：https://github.com/$REPO"
git ls-remote origin 2>/dev/null | head -5 || true
