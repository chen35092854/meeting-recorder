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

    # 应用启动即后台预热识别模型（whisper + 声纹），消除首次转写延迟
    from app.pipeline import preload_models
    preload_models(cfg)

    import tkinter as tk
    from app.gui import RecorderApp

    root = tk.Tk()
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
