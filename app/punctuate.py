# -*- coding: utf-8 -*-
"""中文转写断句恢复（2026-10-19 新增）。

背景：whisper-small（MLX）对 VAD 切成 ~6-12s 的短音频块的中文输出
通常不带任何空格/标点，实时转写与落盘全文连成一长串，可读性差；
实测整段（~70s）喂给模型时 Whisper 会用空格自然断句，短块则没有。
本模块作为 ASR 输出的统一后处理，把断句信号恢复出来：

1. 保留 Whisper 输出的空格信号：相邻两个汉字之间的空格 → 全角逗号
   （英文单词两侧的空格保留，避免破坏中英混排）；
2. 对仍无分隔的长串文本，按「多字从句引导词」保守补逗号
   （引导词前 ≥6 字符且近处无标点才插入）；
3. 句末无标点时补句号；清理重复标点。

所有规则保守（宁可少加、不错加），可通过 config `asr.punctuate` 开关。
"""
from __future__ import annotations

import re

# 多字从句引导词（降低单字误插风险；排序不影响正确性）
_CLAUSE_OPENERS = [
    "因为", "所以", "但是", "不过", "如果", "假如", "那么",
    "然后", "之后", "后来", "接着", "或者", "以及", "另外",
    "同时", "例如", "比如", "如何", "怎么", "是不是",
    "可不可以", "为什么", "现在", "接下来", "未来",
    "最终", "最后", "首先", "其次", "还有",
]

_TERMINAL = set("。！？….,.!?;；")
_BOUNDARY = set("，。！？；、")


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def restore(text: str) -> str:
    """恢复断句。输入 ASR 段的原始文本，输出带中文标点的文本。"""
    if not text:
        return text

    # 1) 空白归一：多空格合并为单空格
    t = re.sub(r"\s+", " ", text).strip()

    # 2) 汉字之间的空格 → 全角逗号（Whisper 中文常用空格代替逗号/停顿）
    chars = list(t)
    for i in range(1, len(chars) - 1):
        if chars[i] == " " and _is_cjk(chars[i - 1]) and _is_cjk(chars[i + 1]):
            chars[i] = "，"
    t = "".join(chars)

    # 3) 从句引导词前补逗号（无任何断句时的兜底）
    t = _insert_pause(t)

    # 4) 句末补句号（已有终止标点则不重复）
    if t and t[-1] not in _TERMINAL:
        t += "。"

    # 5) 清理重复/相邻标点
    t = re.sub(r"([，。！？；])\1+", r"\1", t)
    t = re.sub(r"[,，]\s*[，,]+", "，", t)
    return t


def _insert_pause(t: str) -> str:
    """在从句引导词前插入「，」。保守条件：
    - 引导词不在句首（前 ≥6 字符）；
    - 上一字符不是标点；
    - 前 6 字符内没有句界标点（说明此处确需停顿）。
    """
    positions: set[int] = set()
    for opener in _CLAUSE_OPENERS:
        for m in re.finditer(re.escape(opener), t):
            pos = m.start()
            if pos < 6:
                continue
            if t[pos - 1] in _BOUNDARY:
                continue
            window = t[max(0, pos - 6):pos]
            if any(c in window for c in "，。；"):
                continue
            positions.add(pos)
    if not positions:
        return t
    for pos in sorted(positions, reverse=True):
        t = t[:pos] + "，" + t[pos:]
    return t
