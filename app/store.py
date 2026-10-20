# -*- coding: utf-8 -*-
"""存档：录音 WAV、转写全文 Markdown、转写 JSON、状态文件。

目录结构：
  meeting-recorder/recordings/
    {meeting_id}.wav             原始录音（16kHz 单声道 int16）
    {meeting_id}_transcript.json 结构化转写（供 AI 生成纪要）
    {meeting_id}_transcript.md   转写全文 Markdown（供人工核对）
    current_status.json          当前任务状态（GUI 与智能体协作的桥）
"""
from __future__ import annotations

import json
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config as cfg_mod

RECORDINGS_DIR = cfg_mod.APP_DIR / "recordings"
STATUS_PATH = RECORDINGS_DIR / "current_status.json"

LANG_LABELS = {
    "zh": "中文", "en": "English", "ja": "日本語", "ko": "한국어",
    "fr": "Français", "de": "Deutsch", "es": "Español", "ru": "Русский",
    "pt": "Português", "it": "Italiano", "ar": "العربية", "hi": "हिन्दी",
    "th": "ไทย", "vi": "Tiếng Việt",
}


def lang_label(code: str | None) -> str:
    if not code:
        return "中文"
    return LANG_LABELS.get(code, code)


def ts_fmt(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def save_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    if audio.size == 0:
        audio = np.zeros(1, dtype=np.int16)
    data = np.clip(audio * 32767, -32768, 32767).astype(np.int16) if audio.dtype == np.float32 \
        else audio.astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(data.tobytes())


def build_transcript_md(meta: dict, utterances: list[dict]) -> str:
    lines = [
        f"# 转写全文 · {meta['title']}",
        "",
        f"- 会议时间：{meta['started_at']:%Y-%m-%d %H:%M}（时长 {int(meta['duration'] // 60)} 分钟）",
        f"- 参会人（声纹识别）：{'、'.join(meta.get('speakers') or []) or '—'}",
        "- 语言：按发言人实际语种输出（中文为简体）",
        "- 生成方式：WorkBuddy AI 会议录音 · 本地语音识别",
        "",
        "## 逐句发言记录",
        "",
    ]
    for u in utterances:
        lang = f"（{lang_label(u.get('lang'))}）" if u.get("lang") not in (None, "", "zh") else ""
        lines.append(f"- `[{ts_fmt(u['start'])}]` **{u['speaker']}**{lang}：{u['text']}")
    return "\n".join(lines).rstrip() + "\n"


def save_artifacts(cfg: dict, meta: dict, audio: np.ndarray, utterances: list[dict]) -> dict:
    """保存 WAV/JSON/MD，返回 paths。"""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    meeting_id = meta["meeting_id"]
    paths = {"base": str(RECORDINGS_DIR / meeting_id)}

    if cfg["output"].get("save_wav", True):
        wav = RECORDINGS_DIR / f"{meeting_id}.wav"
        save_wav(wav, audio, cfg["audio"]["sample_rate"])
        paths["wav"] = str(wav)

    j = RECORDINGS_DIR / f"{meeting_id}_transcript.json"
    serialized = dict(meta)
    if isinstance(serialized.get("started_at"), datetime):
        serialized["started_at"] = serialized["started_at"].isoformat()
    if isinstance(serialized.get("ended_at"), datetime):
        serialized["ended_at"] = serialized["ended_at"].isoformat()
    j.write_text(json.dumps({**serialized, "utterances": utterances}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    paths["json"] = str(j)

    md = RECORDINGS_DIR / f"{meeting_id}_transcript.md"
    md.write_text(build_transcript_md(meta, utterances), encoding="utf-8")
    paths["md"] = str(md)
    return paths


def write_status(d: dict) -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def read_status() -> dict | None:
    try:
        if STATUS_PATH.exists():
            return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return None
