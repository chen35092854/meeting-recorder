# -*- coding: utf-8 -*-
"""集成回放：用真实 RecorderApp 类（修复后）处理 142937 事件流，验证无累积重复。

原理：实例化 RecorderApp（窗口 withdraw），不进入 mainloop，直接把
real_events 里的 (kind, payload) 通过 _handle 喂进去（等价于 _poll 取队列），
最后对比 Text 内容与落盘最终 utterances。
"""
from __future__ import annotations

import json
import sys
import tkinter as tk

sys.path.insert(0, ".")

from app.gui import RecorderApp  # noqa: E402
from app.pipeline import SPEAKER_COLORS  # noqa: E402

CFG = {
    "gui": {},
    "audio": {"sample_rate": 16000, "chunk_seconds": 6, "min_zcr": 0.012},
    "asr": {"backend": "mlx", "language": "zh"},
    "vad": {"enabled": True},
    "diarize": {"enabled": True, "merge_gap": 3.0, "max_utt_chars": 200},
    "output": {"obsidian_dir": "/tmp"},
}


def rebuild_events(fid: str) -> tuple[list[tuple[str, dict]], list[str]]:
    d = json.load(open(f"recordings/{fid}_transcript.json"))
    uts = d["utterances"]
    events: list[tuple[str, dict]] = []
    merge_gap = 3.0
    max_chars = 200
    replay: list[dict] = []
    for utt in uts:
        u0 = dict(utt)
        u0["_color"] = SPEAKER_COLORS[0]
        last = replay[-1] if replay else None
        if (last is not None and last["speaker"] == utt["speaker"]
                and utt["start"] - last["end"] < merge_gap):
            combined = last["text"].rstrip() + utt["text"].lstrip()
            if len(combined) <= max_chars:
                last["text"] = combined
                last["end"] = utt["end"]
                events.append(("utt_merge", dict(last, _color=SPEAKER_COLORS[0])))
                continue
        replay.append(u0)
        events.append(("utt", u0))
    return events, [u["text"] for u in replay]


def main() -> None:
    fid = sys.argv[1] if len(sys.argv) > 1 else "20261019_142937"
    root = tk.Tk()
    root.withdraw()
    app = RecorderApp(root, CFG)
    root.update_idletasks()

    events, final_texts = rebuild_events(fid)
    print(f"{fid}: {len(events)} 事件 -> 最终 {len(final_texts)} 条保障 utterance")
    for kind, payload in events:
        app._handle(kind, payload)
    app._redraw()  # 等价 _poll 末尾的统一重绘
    root.update_idletasks()

    disp = app.text.get("1.0", "end-1c")
    print("\n===== 修复后 GUI 显示 =====")
    print(disp)
    # 从显示内容提取所有转写行文本（"  缩进 + 时间戳 + 文本"），与最终 utterances 逐行精确对齐。
    # 不用子串计数：真实重复念词（如"7.6倍"×2）在 transcript 中本就是独立 utterance，
    # 显示多次是正确的，不能当假阳性报错。
    import re
    # 转写行格式："  14:29:38   发言人1   文本" —— 提取时跳过时间戳与发言人前缀
    utt_line_re = re.compile(r"^\s{2}\d{2}:\d{2}:\d{2}\s+发言人\d+\s+(.*?)\s*$")
    shown_texts = [m.group(1) for l in disp.split("\n") if (m := utt_line_re.match(l))]
    if shown_texts == final_texts:
        print(f"\n转写行数={len(shown_texts)}，期望={len(final_texts)} "
              f"✓ 完全一致（逐行精确匹配，无重复无缺少）")
    else:
        print(f"\n转写行数={len(shown_texts)}，期望={len(final_texts)} ✗ 仍有差异")
        from collections import Counter
        sc, fc = Counter(shown_texts), Counter(final_texts)
        for t in sorted(set(sc) | set(fc)):
            if sc[t] != fc[t]:
                print(f"  {sc[t]}显示 vs {fc[t]}期望 行 | {t[:40]}")
    root.destroy()


if __name__ == "__main__":
    main()
