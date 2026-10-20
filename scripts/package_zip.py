#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包发布 zip（macOS 便携版）。

用法：
    python scripts/package_zip.py [版本号，默认 v1.0.2]

要点：
- 来源目录：releases/test-package/（发布前在此目录完成内容清洗）
- 中文文件名设置 UTF-8 标志位（zipfile flag_bits 0x800），解压不乱码
- 保留 Unix 可执行位：install.sh 为 0755
- 跳过 __pycache__ / *.pyc / .DS_Store / .env / 空目录
- 发布前检查敏感内容：绝对用户路径、.env 密钥文件
"""
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "releases" / "test-package"
VER = sys.argv[1] if len(sys.argv) > 1 else "v1.0.2"
OUT = ROOT / "releases" / f"meeting-recorder-mac-{VER}-测试包-{date.today():%Y%m%d}.zip"

SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIX = {".pyc", ".DS_Store"}
SENSITIVE = ("/Users/eric",)  # 绝对用户路径黑名单


def build_entries():
    for p in sorted(APP.rglob("*")):
        if p.is_dir():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in SKIP_SUFFIX or p.name == ".DS_Store":
            continue
        if p.name == ".env":
            continue
        yield p


def main() -> int:
    if not APP.is_dir():
        print(f"✖ 来源目录不存在: {APP}")
        return 1

    # 1) 敏感内容扫描（阻止带本机路径的包发出去）
    issues = []
    for p in build_entries():
        if p.suffix in {".py", ".sh", ".md", ".txt", ".command", ".json", ".example"} or ".example" in p.name:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for key in SENSITIVE:
                if key in text:
                    issues.append(f"{p.relative_to(APP)} 包含 {key}")
    if issues:
        print("✖ 检测到敏感内容，已中止打包：")
        for i in issues:
            print("   -", i)
        return 1

    # 2) 打包
    if OUT.exists():
        OUT.unlink()
    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in build_entries():
            name = p.relative_to(APP).as_posix()
            zi = zipfile.ZipInfo()
            zi.filename = name
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.date_time = time.localtime(p.stat().st_mtime)[:6]
            zi.flag_bits |= 0x800  # UTF-8 文件名
            zi.external_attr = (0o755 if name == "install.sh" else 0o644) << 16
            zi.create_system = 3  # unix
            with p.open("rb") as f:
                zf.writestr(zi, f.read())
            count += 1
            print(f"  + {name}")

    # 3) 校验
    with zipfile.ZipFile(OUT) as zf:
        names = zf.namelist()
        bad_utf8 = [n for n in names if not (zf.getinfo(n).flag_bits & 0x800) and any(ord(c) > 127 for c in n)]
        no_exec = [n for n in names if n == "install.sh" and not (zf.getinfo(n).external_attr >> 16) & 0o111]
    if bad_utf8:
        print("✖ 存在未设 UTF-8 标志的中文文件名:", bad_utf8)
        return 1
    if no_exec:
        print("✖ install.sh 缺少可执行位")
        return 1

    size = OUT.stat().st_size
    print(f"\n✔ 打包完成: {OUT}  ({count} 个文件, {size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
