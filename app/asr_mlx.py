# -*- coding: utf-8 -*-
"""基于 Apple MLX 的 Whisper 识别后端（Apple Silicon 加速）。

替换 faster-whisper CPU int8：在 M 系列芯片上速度更快、准确率更高，
且支持中文简体、多语种自动识别。
"""
from __future__ import annotations

import logging
import os

import numpy as np
from huggingface_hub import snapshot_download

from . import asr as asr_mod
from . import model_download as mdl
from . import punctuate as punc_mod

logger = logging.getLogger(__name__)

_CACHE: dict[str, str] = {}


def get_model(cfg: dict) -> str:
    """返回 mlx-whisper 模型的本地绝对路径（优先本地缓存，无缓存再下载）。"""
    name = cfg["asr"].get("mlx_model", "mlx-community/whisper-small-mlx")
    if name in _CACHE:
        return _CACHE[name]
    # 已是本地路径
    if os.path.isdir(name):
        _CACHE[name] = os.path.abspath(name)
        return _CACHE[name]
    # 人工下载优先:主权重已导入本地目录时直接使用本地模型
    local_dir = mdl.local_asr_dir(cfg)
    if local_dir:
        _CACHE[name] = local_dir
        return local_dir
    # 禁用 Xet + 指镜像（否则镜像下大文件 401，2026-10-21 同事实测）
    mdl.setup_env(cfg)
    logger.info("正在准备 MLX 模型: %s", name)
    try:
        # 优先本地缓存，避免运行时联网
        local_path = snapshot_download(repo_id=name, local_files_only=True)
        logger.info("MLX 模型使用本地缓存: %s", local_path)
    except Exception:  # noqa: BLE001
        logger.info("本地无缓存，正在下载 MLX 模型: %s", name)
        local_path = snapshot_download(
            repo_id=name,
            resume_download=True,
            local_files_only=False,
        )
        logger.info("MLX 模型下载完成: %s", local_path)
    _CACHE[name] = local_path
    return local_path


def transcribe(cfg: dict, audio: np.ndarray, sample_rate: int = 16000):
    """识别音频段，返回 [(start, end, text, lang), ...]。

    关键修复（2026-10-19）：
    1. 语言不再 "auto" —— 自动检测对中文短句常误判为 en（日志：
       Detected language 'en' with probability 0.39-0.89），英文解码中文必然乱码。
       默认强制 zh，真正需要多语种时可改 config asr.language=auto。
    2. condition_on_previous_text=False —— 避免噪声段把前文当提示
       产生 T-T-T-T-T / TATA 这类重复幻觉（repetition loop）。
    3. no_speech_threshold 提高 —— 更积极地把无语音的噪声段判为静音跳过。
    4. 后处理过滤：丢弃重复字符/无意义文本。
    """
    import mlx_whisper

    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)
    if len(audio.shape) > 1:
        audio = audio.reshape(-1)
    # 响度归一化：过弱信号（rms<0.05）Whisper 极易幻觉，统一放大到峰值 0.35 左右
    audio = _normalize_loudness(audio)

    model = get_model(cfg)
    cfg_asr = cfg["asr"]
    lang = (cfg_asr.get("language") or "zh").strip().lower()
    if lang in ("", "auto"):
        lang = "zh"  # 中文会议默认；多语种需求改回 auto 有误判风险
    initial_prompt = cfg_asr.get("initial_prompt") or None  # 不设中文引导语，避免弱信号段被模型输出 prompt 残影
    # whisper 判静音：no_speech_prob > no_speech_threshold 且 logprob < logprob_threshold。
    # 用更保守（略低）的阈值让噪声段更容易被判为静音并跳过，减少幻觉。
    no_speech_threshold = float(cfg_asr.get("no_speech_threshold", 0.55))
    logprob_threshold = float(cfg_asr.get("logprob_threshold", -0.8))

    # mlx_whisper 内部会重采样到 16kHz，但输入最好已经是 16kHz mono float32
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=model,
        verbose=False,
        language=lang,
        condition_on_previous_text=False,
        initial_prompt=initial_prompt,
        no_speech_threshold=no_speech_threshold,
        logprob_threshold=logprob_threshold,
    )

    segments = []
    detected = result.get("language") or lang or "zh"
    if not result.get("segments"):
        logger.info("Whisper 未输出任何段（整段无语音/静音）")
    for seg in result.get("segments", []):
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))
        text = (seg.get("text") or "").strip()
        if not text:
            continue

        # ---- 段级置信度过滤（2026-10-19 新增，针对 MLX Whisper）----
        # 说明：mlx_whisper 的 segment 带 no_speech_prob / avg_logprob /
        # compression_ratio 字段。音乐/系统声音常被解码为"字幕by索兰娅"等
        # 幻觉文本，但此时模型认为"无语音"的概率高（no_speech_prob 大）、
        # 平均对数概率极低（avg_logprob 小）、压缩率异常（重复文本）。
        ns_prob = float(seg.get("no_speech_prob", 0.0))
        avg_lp = float(seg.get("avg_logprob", 0.0))
        comp = float(seg.get("compression_ratio", 0.0))
        dropped = False
        if ns_prob > 0.6 and avg_lp < -0.8:
            logger.info("段过滤[无语音] no_speech=%.2f logprob=%.2f %r",
                        ns_prob, avg_lp, text[:60])
            dropped = True
        elif comp > 2.4:  # OpenAI 官方：压缩率 >2.4 判为重复文本
            logger.info("段过滤[压缩率异常] comp=%.2f %r", comp, text[:60])
            dropped = True
        if dropped:
            continue
        if _is_garbage(text):
            logger.info("过滤幻觉文本: %r", text[:60])
            continue
        seg_lang = seg.get("language") or detected
        if cfg_asr.get("simplify_zh") and seg_lang and str(seg_lang).startswith("zh"):
            text = asr_mod.simplify_zh(text, seg_lang)
        # 断句恢复（2026-10-19）：whisper 短块中文常用不带空格/标点，
        # 空格→逗号 + 从句引导词补标点 + 句末句号；可配置关闭
        if cfg_asr.get("punctuate", True):
            text = punc_mod.restore(text)
        segments.append((start, end, text, seg_lang))
    return segments


def _normalize_loudness(audio: np.ndarray, target_peak: float = 0.5,
                        max_gain: float = 20.0) -> np.ndarray:
    """弱信号响度归一化：不足 target_peak 时统一放大到 target_peak。

    修复（2026-10-19 11:35）：用户远场/轻声录音实测 peak 仅 0.02~0.14
    （近场 TTS 对照 0.6），旧逻辑 peak<=0.0875 直接不放大，导致大量
    弱人声段继续按弱信号送入 Whisper → 全部幻觉。现在只要不是真无声
    （peak>=0.005）就放大，上限提高到 20 倍；配合 VAD 双条件过滤 +
    Whisper 置信度过滤（no_speech_prob/compression_ratio）+ 幻觉模式库
    兜底，不会把纯噪声放大成有效输出。
    """
    peak = float(np.max(np.abs(audio), initial=0.0))
    if peak < 0.005:          # 真无声，交由 VAD/静音逻辑处理
        return audio
    if peak < target_peak:
        gain = target_peak / peak
        if gain > max_gain:
            gain = max_gain
        return audio * gain
    return audio


_REPEAT_PATTERNS = [
    # 单个字符连续重复 >= 4 次（T-T-T-T / TATATATA / 僧伽罗文重复等）
    r"(.)\1{3,}",
    # 带连字符的重复如 T-T-T-T
    r"(?:([A-Za-z\u4e00-\u9fff\u0d80-\u0dff])-\1){2,}",
]

# Whisper 对视频/YouTube 音频的典型幻觉（实测"字幕by索兰娅"出现 5 次）
_HALLUCINATION_PATTERNS = [
    r"字幕\s*(?:by|by\s*[:：])",     # 字幕by索兰娅
    r"请\s*使用\s*简体中文\s*转写",   # initial_prompt 残影（已禁用 prompt 仍保险拦截）
    r"感谢\s*(?:大家)?\s*的?\s*(?:收看|观看|点赞)",  # 视频结尾台词式幻觉
    r"订阅\s*(?:我的)?\s*频道",
]


def _is_garbage(text: str) -> bool:
    """判断是否为重复/幻觉文本：整句其实只是同一字的反复。"""
    import re
    if len(text) < 4:
        return False
    compact = text.replace(" ", "").replace("-", "").replace("，", "").replace(",", "")
    if not compact:
        return True
    # 视频/YouTube 幻觉模式（字幕by索兰娅等）
    for pat in _HALLUCINATION_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return True
    # 去重后字符种类极少 => 基本是重复
    unique = len(set(compact))
    if unique <= 2 and len(compact) >= 6:
        return True
    for pat in _REPEAT_PATTERNS:
        if re.search(pat, compact):
            # 需确认重复部分占绝对多数
            return True
    return False


def preload(cfg: dict) -> None:
    """后台预热：触发模型下载/加载。"""
    def _run():
        try:
            get_model(cfg)
            logger.info("MLX Whisper 模型路径已就绪: %s", get_model(cfg))
        except Exception as e:  # noqa: BLE001
            logger.warning("MLX Whisper 预热失败: %s", e)
    import threading
    threading.Thread(target=_run, daemon=True, name="mlx-preload").start()
