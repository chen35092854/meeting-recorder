# -*- coding: utf-8 -*-
"""语音活动检测：Silero VAD（首选）+ 能量/峰值兜底。

作用：
1. 把有人说话的片段从连续音频中切出来，避免静音块被 ASR 幻觉成"Han Han"等乱码；
2. 控制单段最大长度，避免 ASR 在长段上丢字。
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

_SILERO_MODEL = None


def _load_silero():
    global _SILERO_MODEL
    if _SILERO_MODEL is None:
        from silero_vad import load_silero_vad
        _SILERO_MODEL = load_silero_vad()
    return _SILERO_MODEL


def _energy_split(audio: np.ndarray, sample_rate: int,
                  min_speech: float, min_silence: float,
                  max_segment: float,
                  threshold_rms: float = 0.008,
                  threshold_peak: float = 0.03) -> list[tuple[int, int]]:
    """纯能量兜底：rms 与峰值**同时**超过阈值才视为语音。

    阈值比分块静音判断更严：环境噪音（风扇/空调/电脑底噪）通常
    rms 0.005~0.02、peak 峰值偶发 0.02~0.09，若不双条件会整段被判
    为语音送入 ASR，产生 T-T-T 这类幻觉。
    """
    step = int(0.02 * sample_rate)
    if step < 1 or len(audio) < step:
        return []
    speech = []
    for i in range(0, len(audio) - step, step):
        seg = audio[i:i + step]
        rms = float(np.sqrt(np.mean(seg ** 2)))
        peak = float(np.max(np.abs(seg)))
        speech.append(rms > threshold_rms and peak > threshold_peak)
    # 合并连续语音 + 过滤短静音
    segs: list[tuple[int, int]] = []
    start: int | None = None
    silence_frames = 0
    silence_frames_limit = max(1, int(min_silence / 0.02))
    for i, sp in enumerate(speech):
        if sp:
            if start is None:
                start = i * step
            silence_frames = 0
        else:
            if start is not None:
                silence_frames += 1
                if silence_frames >= silence_frames_limit:
                    segs.append((start, i * step - silence_frames_limit * step))
                    start = None
                    silence_frames = 0
    if start is not None:
        segs.append((start, len(audio)))

    # 过滤短语音、切分长语音
    min_samples = int(min_speech * sample_rate)
    max_samples = int(max_segment * sample_rate)
    out: list[tuple[int, int]] = []
    for s, e in segs:
        if e - s < min_samples:
            continue
        while e - s > max_samples:
            out.append((s, s + max_samples))
            s += max_samples
        out.append((s, e))
    return out


def split_speech(audio: np.ndarray, sample_rate: int = 16000,
                 min_speech: float = 0.5, min_silence: float = 0.5,
                 max_segment: float = 10.0,
                 silero_threshold: float = 0.6) -> list[tuple[int, int]]:
    """返回 [(start_sample, end_sample), ...] 语音段。

    silero_threshold: Silero 语音概率门槛，默认 0.6（比官方 0.5 更严格，
    减少把环境噪音误判为语音导致的幻觉）。
    """
    if len(audio) < int(min_speech * sample_rate):
        return _energy_split(audio, sample_rate, min_speech, min_silence, max_segment)

    try:
        model = _load_silero()
        from silero_vad import get_speech_timestamps
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        timestamps = get_speech_timestamps(
            audio,
            model,
            sampling_rate=sample_rate,
            threshold=silero_threshold,
            min_speech_duration_ms=int(min_speech * 1000),
            min_silence_duration_ms=int(min_silence * 1000),
            return_seconds=False,
        )
        segs = [(int(t["start"]), int(t["end"])) for t in timestamps]
        if not segs:
            # Silero 可能把非真实语音或特殊噪声判为静音；若能量明显有声则兜底
            rms = float(np.sqrt(np.mean(audio ** 2)))
            peak = float(np.max(np.abs(audio), initial=0.0))
            if rms > 0.008 and peak > 0.03:
                return _energy_split(audio, sample_rate, min_speech, min_silence, max_segment)
            return []
        # 切分超长段
        max_samples = int(max_segment * sample_rate)
        out: list[tuple[int, int]] = []
        for s, e in segs:
            while e - s > max_samples:
                out.append((s, s + max_samples))
                s += max_samples
            out.append((s, e))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("Silero VAD 失败，回退能量 VAD: %s", e)
        return _energy_split(audio, sample_rate, min_speech, min_silence, max_segment)


def preload() -> None:
    """后台预热 VAD 模型。"""
    def _run():
        try:
            _load_silero()
            logger.info("Silero VAD 预热完成")
        except Exception as e:  # noqa: BLE001
            logger.warning("Silero VAD 预热失败: %s", e)
    import threading
    threading.Thread(target=_run, daemon=True, name="vad-preload").start()
