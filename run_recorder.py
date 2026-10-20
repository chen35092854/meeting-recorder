#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入口：启动会议录音 GUI。

用法：
    python run_recorder.py [--title 会议主题]
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy 会议录音 · 实时转写与 AI 纪要")
    ap.add_argument("--title", default="", help="会议主题（可在 GUI 中修改）")
    args = ap.parse_args()

    from app import config
    cfg = config.load()

    log_dir = config.APP_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(str(log_dir / "recorder.log"), encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.info("启动会议录音：%s", args.title)

    # 首次运行：先弹 4 步配置向导（环境检查 / AI 纪要 / 保存目录），
    # 完成或关闭后清空向导控件，继续构建主界面
    try:
        import tkinter as tk
        from app.setup_wizard import SetupWizard, needs_first_run
    except ModuleNotFoundError as e:
        if e.name in ("tkinter", "_tkinter"):
            # 大多数情况 install.sh 已提前拦截；直接 python 运行本文件时兜底
            print("\n✖ 当前 Python 缺少 Tk 图形库（_tkinter），无法显示录音界面。")
            print("  常见原因与解决：")
            print("  - Homebrew 安装的 Python 默认不带 Tk：")
            print("      brew install python-tk@<版本>   （如 python@3.11 则装 python-tk@3.11）")
            print("  - 或改用 python.org 官方 Python（自带 Tk 支持）：")
            print("      https://www.python.org/downloads/")
            print("  安装后删除 .venv 文件夹，重新双击 install.sh 即可。\n")
            return 1
        raise

    root = tk.Tk()
    if needs_first_run():
        wiz = SetupWizard(root, cfg)
        root.mainloop()          # 向导驱动事件循环；完成/关闭后返回
        wiz.dispose()
        cfg = config.load()      # 向导可能已修改配置（LLM/保存目录），重新加载

    # 应用启动即后台预热识别模型（whisper + 声纹），消除首次转写延迟
    from app.pipeline import preload_models
    preload_models(cfg)

    from app.gui import RecorderApp

    RecorderApp(root, cfg)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.exit(1)
