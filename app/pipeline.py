# -*- coding: utf-8 -*-
"""会议处理管线：录音分块 -> 转写 -> 声纹分人 -> 展示推送 -> 结束时落盘。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import numpy as np

from . import diarize as diarize_mod
from . import model_download as mdl
from . import store
from . import vad
from .diarize import SpeakerRegistry
from .recorder import create_recorder

logger = logging.getLogger(__name__)

SPEAKER_COLORS = [
    "#c0392b", "#2471a3", "#1e8449", "#7d3c98",
    "#d35400", "#148f77", "#1f618d", "#92701b",
]


def _load_asr(cfg: dict):
    """按配置加载 ASR 模块：mlx（默认）或 faster-whisper（兼容回退）。"""
    backend = cfg["asr"].get("backend", "mlx")
    if backend == "mlx":
        from . import asr_mlx
        return asr_mlx
    from . import asr as asr_mod
    return asr_mod


def preload_models(cfg: dict) -> None:
    """应用启动时后台准备识别模型（GUI 可感知进度）。

    v1.0.11 改进（2026-10-21，同事机器修复）：
    此前模型下载走 hugggingface_hub 默认 Xet 协议 → 镜像下 401 → ASR 预热
    静默失败 → 转写区永远不出字且无进度提示。现在：
    1. 缺失模型先**带进度**下载（写全局 DownloadState，gui 轮询展示）；
    2. 下载完成再加载 whisper / 声纹 / VAD，消除首次转写的冷等待。
    """
    def _run():
        from . import model_download as mdl
        mdl.resolve_backend(cfg)   # Intel 机切 faster-whisper（防双份下载）
        mdl.setup_env(cfg)
        st = mdl.DownloadState.get()
        try:
            # 缺失模型先下载（内部自动跳过已缓存项）
            mdl.ensure_missing(cfg, state=st)
        except Exception as e:  # noqa: BLE001
            logger.warning("识别模型下载未完成: %s", e)
        asr_mod = _load_asr(cfg)
        if not mdl.asr_ready(cfg):
            logger.warning("语音识别模型未就绪，转写将不可用（请检查网络后重启）")
        try:
            asr_mod.get_model(cfg)
            logger.info("ASR 模型预热完成 (%s)", cfg["asr"].get("backend", "mlx"))
        except Exception as e:  # noqa: BLE001
            logger.warning("ASR 预热失败: %s", e)
        if cfg["diarize"].get("enabled", True):
            try:
                diarize_mod.get_encoder(cfg["diarize"]["model"])
                logger.info("声纹模型预热完成")
            except Exception as e:  # noqa: BLE001
                logger.warning("声纹模型预热失败: %s", e)
        if cfg.get("vad", {}).get("enabled", True):
            try:
                vad.preload()
            except Exception as e:  # noqa: BLE001
                logger.warning("VAD 预热失败: %s", e)
    threading.Thread(target=_run, daemon=True, name="model-preload").start()


def _zero_crossing_rate(audio: np.ndarray, sample_rate: int) -> float:
    """粗略过零率：相邻样本符号翻转次数 / 样本数。

    真实人声（两侧存在清/浊音交替）通常在 0.1~0.4；
    低频连续信号、音乐、环境底噪通常 < 0.05。
    用于把非人声段在送 ASR 前拦截，避免 Whisper 幻觉与伪发言人。
    """
    if audio.size < 64:
        return 0.0
    a = audio.astype(np.float32)
    # 去除直流偏置后再数过零
    a = a - a.mean()
    signs = np.signbit(a)
    flips = np.count_nonzero(signs[1:] != signs[:-1])
    return flips / (a.size - 1)


class MeetingSession:
    """一次会议：启动录音 -> 实时产出逐句记录（含发言人）-> 结束保存。"""

    def __init__(self, cfg: dict, title: str = "", device=None, ui_q=None):
        self.cfg = cfg
        self.title = title.strip() or "会议"
        self.device = device
        self.ui_q = ui_q                     # GUI 消息队列，None 则无界面模式
        a = cfg["asr"]
        self.rec = create_recorder(
            sample_rate=cfg["audio"]["sample_rate"],
            chunk_seconds=a.get("chunk_seconds", 15),
            device=device,
            channels=cfg["audio"].get("channels", 1),
        )
        self.registry = SpeakerRegistry(threshold=cfg["diarize"]["threshold"]) \
            if cfg["diarize"].get("enabled", True) else None
        self.meeting_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.started_at = datetime.now()
        self.utterances: list[dict] = []
        self._lock = threading.Lock()
        self._stop = False
        self._proc: threading.Thread | None = None
        self.speaker_serial = 0  # 仅用于实时展示的编号

    # ---------- 生命周期 ----------
    def input_level(self) -> tuple[float, float]:
        """当前输入电平 (rms, peak)，供 GUI 实时显示。未开始录音时返回 (0, 0)。"""
        if self.rec is None:
            return 0.0, 0.0
        return float(self.rec.current_rms), float(self.rec.current_peak)

    def start(self) -> None:
        store.write_status({
            "state": "recording",
            "meeting_id": self.meeting_id,
            "title": self.title,
            "started_at": self.started_at.isoformat(),
            "progress": 5,
            "stage": "录音进行中…",
            "transcript_path": None,
            "minutes_path": None,
            "error": None,
        })
        self.rec.start()
        logger.info("录音已启动：后端=%s 设备=%s 分块=%ss",
                    type(self.rec).__name__, getattr(self.rec, "device", "默认"),
                    self.cfg["asr"].get("chunk_seconds", 15))
        self._proc = threading.Thread(target=self._process, daemon=True, name="asr-worker")
        self._proc.start()
        self._post("sys", "recording")

    def _process(self) -> None:
        asr_mod = _load_asr(self.cfg)
        backend = self.cfg["asr"].get("backend", "mlx")
        first_call = True
        prev_speaker = "发言人1"
        while True:
            if self._stop:
                break
            try:
                t0, audio = self.rec.drain_chunk(timeout=0.4)
            except Exception:  # queue.Empty
                continue
            if self._stop or len(audio) == 0:
                break
            rms = float(np.sqrt(np.mean(audio ** 2)))
            peak = float(np.max(np.abs(audio), initial=0.0)) if audio.size else 0.0
            logger.info("音频分块：偏移=%.1fs rms=%.5f peak=%.5f 样本=%d",
                        t0 - self.rec.start_time, rms, peak, len(audio))

            # ---- VAD：只把有人说话的子段送去识别，避免静音幻觉 ----
            vcfg = self.cfg.get("vad", {})
            if vcfg.get("enabled", True):
                speech_segs = vad.split_speech(
                    audio, self.rec.sample_rate,
                    min_speech=vcfg.get("min_speech", 0.5),
                    min_silence=vcfg.get("min_silence", 0.5),
                    max_segment=vcfg.get("max_segment", 10.0),
                    silero_threshold=vcfg.get("silero_threshold", 0.6),
                )
                if not speech_segs:
                    logger.info("VAD 未检测到有效语音，已跳过")
                    continue
            else:
                speech_segs = [(0, len(audio))]

            for seg_start, seg_end in speech_segs:
                seg_audio = audio[seg_start:seg_end]
                seg_t0 = t0 + seg_start / self.rec.sample_rate
                seg_dur = (seg_end - seg_start) / self.rec.sample_rate

                # ---- 极弱信号快检：只有过零率极低（纯低频底噪/无声）才跳过 ----
                # 注意：远场低音量真实人声 zcr 可低至 0.02~0.09（实测 11:17
                # 用户真实会议 0.018~0.09），绝不能按"音乐"标准(0.05~0.06)拦截，
                # 否则会把真人声全部误杀。非人声/幻觉由 Whisper 置信度过滤
                # （no_speech_prob/avg_logprob/compression_ratio）与幻觉模式库兜底。
                min_zcr = float(self.cfg["audio"].get("min_zcr", 0.012))
                if seg_dur > 0.3 and _zero_crossing_rate(seg_audio, self.rec.sample_rate) < min_zcr:
                    logger.info("段跳过[几乎无声] 偏移=%.1fs zcr=%.3f", seg_t0 - self.rec.start_time,
                                _zero_crossing_rate(seg_audio, self.rec.sample_rate))
                    continue

                if first_call:
                    # v1.0.11：模型未就绪时等待下载完成再转写（不并发下载），
                    # 进度由 GUI 顶部状态条展示（与向导共用 DownloadState）。
                    if not mdl.asr_ready(self.cfg):
                        st_state = mdl.DownloadState.get().snapshot()["state"]
                        if st_state == "running":
                            self._post("sys", "⏳ 识别模型正在下载"
                                               "（请看界面顶部进度），"
                                               "完成后自动开始转写…")
                        else:
                            self._post("sys", "识别模型未就绪，"
                                               "正在准备下载，请稍候…")
                        continue
                    self._post("sys", f"正在加载 {backend} 识别模型，请稍候…")
                try:
                    segments = asr_mod.transcribe(self.cfg, seg_audio, self.rec.sample_rate)
                except Exception as e:  # noqa: BLE001
                    if backend == "mlx":
                        logger.warning("MLX 模型加载/调用失败，自动回退 faster-whisper: %s", e)
                        self._post("sys", "MLX 模型加载失败，自动回退到 faster-whisper，请稍候…")
                        self.cfg["asr"]["backend"] = "faster-whisper"
                        backend = "faster-whisper"
                        asr_mod = _load_asr(self.cfg)
                        try:
                            segments = asr_mod.transcribe(self.cfg, seg_audio, self.rec.sample_rate)
                        except Exception as e2:  # noqa: BLE001
                            self._post("sys", f"识别出错：{e2}")
                            continue
                    else:
                        self._post("sys", f"识别出错：{e}")
                        continue
                finally:
                    first_call = False

                # 无有效转写文本的段（静音/幻觉被过滤掉）不进入分人，
                # 避免噪声段被声纹识别建出伪发言人（曾把单人拆成 7 个）
                if not segments:
                    logger.info("段无有效文本，跳过声纹分人 偏移=%.1fs", seg_t0 - self.rec.start_time)
                    continue

                # ---- 分人：整个 VAD 段只做一次声纹识别，段内子句共用该发言人 ----
                # （之前对每个 ASR 子段分别识别，短段声纹不稳会把一个人拆成多人）
                seg_speaker, seg_color_i = self._identify(
                    seg_audio, 0, seg_dur, prev_speaker
                )
                prev_speaker = seg_speaker
                logger.info("语音段 偏移=%.1fs 时长=%.2fs 发言人=%s 子句=%d",
                            seg_t0 - self.rec.start_time, seg_dur, seg_speaker,
                            len(segments))
                for start, end, text, lang in segments:
                    utt = {
                        "start": seg_t0 + start,
                        "end": seg_t0 + end,
                        "speaker": seg_speaker,
                        "text": text,
                        "lang": lang or "zh",
                    }
                    # ---- 相邻同发言人短段合并（2026-10-19 优化）----
                    # 用户反馈：一句话/一段话被切得稀碎。Whisper 对同一
                    # VAD 段常输出多条短 segment，且停顿 < merge_gap 的相邻
                    # 语音段也是同一人连续发言——把间隔够近的同发言人段
                    # 合并为一条长转写，展示与落盘都更连贯。
                    # 2026-10-19 15:00 追加：① merge_gap 改 3s（停顿超过
                    # 3s 才算新段落）；② max_utt_chars=200——合并后超长则
                    # 不再合并，另起一条（每段最多约 200 字符）。
                    dcfg = self.cfg["diarize"]
                    merge_gap = float(dcfg.get("merge_gap", 3.0))
                    max_chars = int(dcfg.get("max_utt_chars", 200))
                    merged = None
                    with self._lock:
                        last = self.utterances[-1] if self.utterances else None
                        if (last is not None and last["speaker"] == seg_speaker
                                and utt["start"] - last["end"] < merge_gap):
                            combined = last["text"].rstrip() + text.lstrip()
                            if len(combined) <= max_chars:
                                # 拼接文本（去除段落间多余空白，中文自然衔接）
                                last["text"] = combined
                                last["end"] = utt["end"]
                                merged = dict(last)
                        if merged is None:
                            self.utterances.append(utt)
                    if merged is not None:
                        self._post("utt_merge", dict(
                            merged,
                            _color=SPEAKER_COLORS[seg_color_i % len(SPEAKER_COLORS)]))
                    else:
                        self._post("utt", dict(
                            utt,
                            _color=SPEAKER_COLORS[seg_color_i % len(SPEAKER_COLORS)]))

    def _identify(self, audio: np.ndarray, start: float, end: float, prev: str) -> tuple[str, int]:
        if self.registry is None:
            return prev, 0
        if not self.registry.is_ready:
            return prev, 0  # 声纹模型尚未预热完成，暂沿用上一发言人，避免阻塞
        seg_min = self.cfg["diarize"].get("min_speaker_segment", 1.0)
        seg_dur = end - start
        if seg_dur < seg_min:
            return prev, 0
        i0 = int(start * self.rec.sample_rate)
        i1 = min(int(end * self.rec.sample_rate), len(audio))
        clip = audio[i0:i1]
        if clip.size < int(0.4 * self.rec.sample_rate):
            return prev, 0
        try:
            return self.registry.identify(
                clip, self.cfg["diarize"]["model"],
                prev_speaker=prev,
                temporal_threshold=self.cfg["diarize"].get("temporal_threshold", 0.72),
            )
        except Exception:  # noqa: BLE001 声纹模型加载失败时降级为单人
            return prev, 0

    def end(self) -> dict:
        """停止录音，处理积压数据，落盘并写 status=transcript_ready。"""
        self._stop = True
        self.rec.stop()
        if self._proc is not None:
            self._proc.join(timeout=90)
        ended_at = datetime.now()
        audio = self.rec.full_audio()
        duration = max(0.0, (ended_at - self.started_at).total_seconds())
        speakers = []
        for u in self.utterances:  # 保持出现顺序
            if u["speaker"] not in speakers:
                speakers.append(u["speaker"])
        meta = {
            "meeting_id": self.meeting_id,
            "title": self.title,
            "started_at": self.started_at,
            "ended_at": ended_at,
            "duration": duration,
            "speakers": speakers,
        }
        paths = store.save_artifacts(self.cfg, meta, audio, self.utterances)
        store.write_status({
            "state": "transcript_ready",
            "meeting_id": self.meeting_id,
            "title": self.title,
            "started_at": self.started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_seconds": duration,
            "speakers": speakers,
            "progress": 15,
            "stage": "转写已保存，等待 AI 生成纪要：请回到 WorkBuddy 对话发送任意消息（如“生成会议纪要”）",
            "transcript_path": paths.get("json"),
            "transcript_md_path": paths.get("md"),
            "minutes_path": None,
            "minutes_obsidian_path": None,
            "error": None,
        })
        return {"paths": paths, "meta": meta, "utterances": list(self.utterances)}

    # ---------- 内部 ----------
    def _post(self, kind: str, payload) -> None:
        if self.ui_q is not None:
            try:
                self.ui_q.put((kind, payload))
            except Exception:  # noqa: BLE001
                pass
