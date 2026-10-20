# -*- coding: utf-8 -*-
"""语音识别：faster-whisper（本地运行，隐私安全）。"""
from __future__ import annotations

import numpy as np

from . import punctuate as punc_mod

_MODELS = {}  # model_size -> model


def get_model(cfg: dict):
    c = cfg["asr"]
    size = c.get("model_size", "small")
    if size not in _MODELS:
        from faster_whisper import WhisperModel
        _MODELS[size] = WhisperModel(
            size,
            device=c.get("device", "cpu"),
            compute_type=c.get("compute_type", "int8"),
        )
    return _MODELS[size]


def transcribe(cfg: dict, audio: np.ndarray, sample_rate: int = 16000):
    """识别一段 float32 mono 音频，返回 [(start, end, text, lang)]。

    语言策略：默认 auto —— whisper 按音频实际语种输出，
    满足“中文输出简体中文、其他语种按原语种输出”的要求。
    """
    c = cfg["asr"]
    language = None
    if c.get("language") and c["language"] not in ("auto", ""):
        language = c["language"]

    model = get_model(cfg)
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=c.get("beam_size", 5),
        condition_on_previous_text=False,
    )
    out = []
    for s in segments:
        text = (s.text or "").strip()
        if not text:
            continue
        # 断句恢复（2026-10-19）：与 MLX 后端保持一致
        if c.get("punctuate", True):
            text = punc_mod.restore(text)
        lang = getattr(s, "language", None) or info.language
        lang = (lang or "").lower().split("-")[0]
        out.append((float(s.start), float(s.end), text, lang))
    return out


_ZH_LANGS = {"zh", "zh-cn", "zh-tw"}


def simplify_zh(text: str, lang: str) -> str:
    """中文文本统一转为简体（传统/异体转简体）。"""
    try:
        from opencc import OpenCC
        return OpenCC("t2s").convert(text)
    except Exception:  # noqa: BLE001
        return text
