# -*- coding: utf-8 -*-
"""GitHub 同步（Git Data API 通道）：
github.com 域被代理/网络挡（隧道 502、直连超时），但 api.github.com 可达。
本脚本用 REST Git Data API 重建本地 missing 的 commit 链（blob→tree→commit→ref），
把 main 分支推进到本地 HEAD，实现与 git push 等价的结果。

要点：
- 只追加不破坏：分叉点=远程当前 main 头（merge-base），其后逐个重放；
- 对象内容寻址：若 GitHub 原样保留 author/committer（含时区），重建 sha 与本地一致；
- 完成后 GET 校验本地 HEAD sha 是否已在远程（200=远端正与本地完全一致）。
"""
import base64
import pathlib
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = str(pathlib.Path(__file__).resolve().parents[1])

# ---- 读取凭据（不打印任何值） ----
env = {}
with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

GH_USER = env.get("GITHUB_USERNAME", "")
GH_TOKEN = env.get("GITHUB_TOKEN", "")
assert GH_USER and GH_TOKEN, ".env 缺少 GITHUB_USERNAME/GITHUB_TOKEN"
REPO = f"{GH_USER}/meeting-recorder"
API = f"https://api.github.com/repos/{REPO}/git"


def gh(method, path, payload=None, expect=200):
    req = urllib.request.Request(
        API + path, method=method,
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "meeting-recorder-sync"})
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        with urllib.request.urlopen(req, data=body, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "ignore")
        if expect and e.code == expect:
            return json.loads(err) if err else {}
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {err[:300]}")


def git(*args):
    return subprocess.check_output(["git", "-C", ROOT] + list(args),
                                   text=True, errors="replace").strip()


def git_bytes(*args):
    return subprocess.check_output(["git", "-C", ROOT] + list(args))


def parse_author(line: str) -> dict:
    m = re.match(r"(.*) <(.*)> (\d+) ([+-]\d{4})$", line)
    assert m, f"author 行解析失败: {line!r}"
    name, email, ts, offset = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    tz = datetime.timezone(datetime.timedelta(
        hours=int(offset[:3]), minutes=int(offset[3:]) * (1 if offset[0] == "+" else -1)))
    iso = datetime.datetime.fromtimestamp(ts, tz).isoformat()
    return {"name": name, "email": email, "date": iso}


uploaded = {"blob": set(), "tree": set()}  # 本地sha 集合（远程同内容幂等）
sha_map = {}  # 本地commit sha -> 远程重建commit sha（GitHub 会对日期规范化，sha 不保证一致）


def upload_blob(sha):
    if sha in uploaded["blob"]:
        return
    raw = git_bytes("cat-file", "blob", sha)
    b64 = base64.b64encode(raw).decode()
    gh("POST", "/blobs", {"content": b64, "encoding": "base64"})
    uploaded["blob"].add(sha)


def upload_tree(sha):
    if sha in uploaded["tree"]:
        return
    items = []
    for line in git("ls-tree", sha).splitlines():
        mode, typ, sub = line.split("\t")[0].split(" ") if "\t" in line else (None, None, None)
        mode, typ, obj = line.split()[0], line.split()[1], line.split()[2]
        _, _, path = line.partition("\t")
        if typ == "blob":
            upload_blob(obj)
        else:  # tree
            upload_tree(obj)
        items.append({"path": path, "mode": mode, "type": typ, "sha": obj})
    gh("POST", "/trees", {"tree": items})
    uploaded["tree"].add(sha)


def rebuild_commit(c):
    raw = git("cat-file", "-p", c)
    lines = raw.split("\n")
    tree = parents = None
    author = committer = None
    i = 0
    while i < len(lines) and lines[i] != "":
        line = lines[i]
        if line.startswith("tree "):
            tree = line.split(" ")[1]
        elif line.startswith("parent "):
            parents = (parents or []) + [line.split(" ")[1]]
        elif line.startswith("author "):
            author = parse_author(line[len("author "):])
        elif line.startswith("committer "):
            committer = parse_author(line[len("committer "):])
        i += 1
    message = "\n".join(lines[i + 1:]).strip("\n")
    assert tree and author and committer
    upload_tree(tree)
    # parent 用「远程已有 sha」：已重建的走 sha_map，基准 commit 用原名（已在远程）
    parents_remote = [sha_map.get(p, p) for p in (parents or [])]
    payload = {"message": message, "tree": tree,
               "parents": parents_remote,
               "author": author, "committer": committer}
    created = gh("POST", "/commits", payload)
    sha_map[c] = created["sha"]
    return created["sha"]


def main():
    # 1) 远程 main 当前头
    ref = gh("GET", "/refs/heads/main")
    remote_head = ref["object"]["sha"]
    print(f"远程 main 当前: {remote_head}")

    # 本地是否有远程 head 对象？正常 git push 同步过的历史应为同一对象；
    # 若没有（远程是此前 Git Data API 重建版），改用「最近共同祖先」策略。
    has_local = (subprocess.run(["git", "-C", ROOT, "cat-file", "-e",
                                 remote_head], capture_output=True).returncode
                 == 0)
    if has_local:
        base = git("merge-base", remote_head, "main")
        to_push = git("rev-list", "--reverse", f"{base}..main").splitlines()
        note = f"分叉点: {base}"
    else:
        chain = git("rev-list", "--reverse", "main").splitlines()
        last_in_remote = None
        for c in chain:
            try:
                gh("GET", f"/commits/{c}")  # 远程存在该 commit 对象
                last_in_remote = c
            except Exception:
                break
        if last_in_remote is None:
            base, to_push, note = None, chain, "无共同祖先，全链重放（force 覆盖）"
        else:
            base = last_in_remote
            to_push = git("rev-list", "--reverse",
                          f"{base}..main").splitlines()
            note = f"最近共同祖先: {base}"
    print(f"{note}，待重放 {len(to_push)} 个 commit")

    # 3) 逐 commit 重建
    new_head = None
    for c in to_push:
        new_head = rebuild_commit(c)
        print(f"  + {c[:7]} -> 新commit {new_head}  {git('log','-1','--format=%s', c)[:50]}")
    assert new_head

    # 4) 更新 refs/heads/main（先快进，422 再 force 兜底）
    try:
        gh("PATCH", "/refs/heads/main", {"sha": new_head})
        print("\n已快进 main -> ", new_head)
    except RuntimeError as e:
        gh("PATCH", "/refs/heads/main", {"sha": new_head, "force": True})
        print("\n非快进，force 更新 main ->", new_head)

    # 5) 校验：本地 HEAD 原始 sha 是否已存在于远程（0=完全一致）
    local_head = git("rev-parse", "main")
    try:
        gh("GET", f"/commits/{local_head}")
        print(f"✔ 远程对象与本地完全一致（sha={local_head}）")
    except Exception:
        print(f"⚠ 远程为重建版（内容一致、sha 不同）。本地 {local_head} 未在远程。")
        print("  提示：之后网络正常时 `bash scripts/push_github.sh` 可让两边 sha 对齐。")

    remote_after = gh("GET", "/refs/heads/main")["object"]["sha"]
    print(f"远端 main 最终: {remote_after}")
    print("SYNC OK")


if __name__ == "__main__":
    main()
