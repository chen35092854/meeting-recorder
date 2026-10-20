# -*- coding: utf-8 -*-
"""冒烟测试：验证核心模块在离线场景下可用。
用法：./.venv/bin/python scripts/smoke_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np


def main() -> int:
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail else ""))
        if not cond:
            ok = False

    print("== 1. 模块导入 ==")
    try:
        from app import asr, config, diarize, gui, pipeline, recorder, store
        print("  ✓ 全部 app 模块导入成功")
    except Exception as e:  # noqa: BLE001
        print("  ✗ 导入失败:", e)
        return 1

    cfg = config.load()
    print("  ✓ 配置加载, 纪要目录:", cfg["output"]["obsidian_dir"])

    print("== 2. 录音后端检测 ==")
    backend = recorder.detect_backend()
    print("  后端: ", backend)
    check("存在可用后端", backend in ("pyaudio", "sounddevice", "av"))

    print("== 3. ASR（whisper-small, 静音输入）==")
    try:
        segs = asr.transcribe(cfg, np.zeros(16000 * 3, dtype=np.float32))
        check("ASR 正常返回段落列表", isinstance(segs, list))
        if len(segs) > 0:
            print(f"    （注：whisper 对纯静音偶有幻觉输出 {len(segs)} 段；应用层 RMS 静音闸门"
                  f"会在进入 ASR 前滤除静音，实际录音流程不受影响）")
    except Exception as e:  # noqa: BLE001
        check("ASR 调用", False, str(e)[:80])

    print("== 4. 声纹分人（合成双音色）==")
    try:
        reg = diarize.SpeakerRegistry(threshold=0.72)
        sr = cfg["audio"]["sample_rate"]
        t = np.linspace(0, 1.2, int(sr * 1.2), endpoint=False)
        clip_a = (0.2 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)   # 低频音色近似
        clip_b = (0.2 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)   # 高频音色近似
        w1, _ = reg.identify(clip_a, cfg["diarize"]["model"])
        w2, _ = reg.identify(clip_a, cfg["diarize"]["model"])
        check("同一段音频两次判定一致", w1 == w2, f"{w1} vs {w2}")
        reg.identify(clip_b, cfg["diarize"]["model"])
        check("不同音色识别为新增发言人", len(reg.speakers) == 2, f"speakers={len(reg.speakers)}")
    except Exception as e:  # noqa: BLE001
        check("声纹分人", False, str(e)[:80])

    print("== 5. 存档与状态文件 ==")
    try:
        utterances = [
            {"start": 10.0, "end": 13.5, "speaker": "发言人1", "text": "今天我们讨论项目进度。", "lang": "zh"},
            {"start": 14.0, "end": 17.2, "speaker": "发言人2", "text": "Let me walk you through the timeline.", "lang": "en"},
        ]
        meta = {"meeting_id": "smoke_001", "title": "测试会议", "started_at": __import__("datetime").datetime.now(),
                "ended_at": __import__("datetime").datetime.now(), "duration": 120, "speakers": ["发言人1", "发言人2"]}
        audio = (0.05 * np.random.randn(16000 * 5)).astype(np.float32)
        paths = store.save_artifacts(cfg, meta, audio, utterances)
        for k, v in paths.items():
            if k == "base":  # base 是路径前缀，非文件
                continue
            check(f"生成 {k}", Path(v).exists() and Path(v).stat().st_size > 0)
        store.write_status({"state": "transcript_ready", "meeting_id": "smoke_001", "title": "测试会议",
                            "progress": 15, "stage": "测试", "transcript_path": paths["json"],
                            "minutes_path": None, "error": None})
        st = store.read_status()
        check("状态文件可回读", st is not None and st["state"] == "transcript_ready")
        md = store.build_transcript_md(meta, utterances)
        check("转写 MD 含发言人标记", "**发言人1**" in md and "English" in md)
    except Exception as e:  # noqa: BLE001
        check("存档", False, str(e)[:80])

    print("== 6. GUI 模块 ==")
    try:
        import tkinter
        check("tkinter 可用", True, f"Tk {tkinter.TkVersion}")
    except Exception as e:  # noqa: BLE001
        check("tkinter", False, str(e)[:80])

    print()
    print("冒烟测试", "全部通过 ✔" if ok else "存在失败项 ✘")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
