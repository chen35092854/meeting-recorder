# -*- coding: utf-8 -*-
"""声纹说话人识别：SpeechBrain ECAPA-TDNN 嵌入 + 余弦相似度增量聚类。

原理：对每段语音提取说话人嵌入向量，与已知发言人质心比较，
相似度 >= threshold 归入该发言人并在线更新质心，否则新增发言人。
音色不同 => 嵌入差异大 => 自动区分多发言人。
"""
from __future__ import annotations

import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

CACHE_ROOT = os.path.join(os.path.expanduser("~"), ".cache", "workbuddy", "spkrec")

_ENCODER = None  # 模块级单例：预热一次，所有会话共享


def _resolve_local_source(model_name: str) -> str | None:
    """优先返回本地声纹模型目录（魔搭落盘 / 人工下载），否则 None。

    SpeechBrain 的 source 支持本地目录（须含 hyperparams.yaml 才能解析
    embedding_model.ckpt/post_process.ckpt），加载时不联网。与
    model_download.LOCAL_SPK_DIR（~/.cache/workbuddy/spkrec-ms）配合。
    """
    try:
        from . import model_download as mdl
        d = mdl.local_spk_dir({"diarize": {"model": model_name}})
        if d and os.path.isfile(os.path.join(d, "hyperparams.yaml")):
            return d
    except Exception:  # noqa: BLE001 本地目录不可用时回退 HF 源
        pass
    return None


def get_encoder(model_name: str):
    """加载（或复用已缓存的）ECAPA 声纹编码器。

    优先从本地模型目录加载（魔搭/人工通道落盘），缺失或加载失败再回退
    HuggingFace 源（snapshot 缓存）。同一模型只预热一次，全局共享。
    """
    global _ENCODER
    if _ENCODER is None:
        os.makedirs(CACHE_ROOT, exist_ok=True)  # 预建缓存目录，避免 speechbrain mkdir 竞态 EEXIST
        from speechbrain.inference.speaker import EncoderClassifier
        sources = [s for s in (_resolve_local_source(model_name), model_name) if s]
        last_err: Exception | None = None
        for src in sources:
            try:
                _ENCODER = EncoderClassifier.from_hparams(
                    source=src,
                    savedir=CACHE_ROOT,
                    run_opts={"device": "cpu"},
                )
                break
            except FileExistsError:  # 与预热线程并发建目录的竞态：重试一次
                try:
                    _ENCODER = EncoderClassifier.from_hparams(
                        source=src,
                        savedir=CACHE_ROOT,
                        run_opts={"device": "cpu"},
                    )
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("从 %s 加载声纹模型失败，尝试下一源", src)
        if _ENCODER is None:
            raise RuntimeError(f"声纹模型加载失败: {last_err}")
    return _ENCODER


class SpeakerRegistry:
    """增量说话人聚类：历史嵌入最大相似度匹配。

    2026-10-19 优化：单人被拆成多人的根因是 ECAPA 对 1~3s 短段的嵌入
    抖动大，只与「单个质心」比较时相似度常掉到阈值以下 → 误建新发言人。
    改为每个发言人保存最近 MAX_HIST 个嵌入，新段取「与全部历史嵌入的
    最大相似度」匹配——其中任一次足够像即归并，短段抖动不再导致拆人。
    """

    MAX_HIST = 8  # 每个发言人的历史嵌入窗口（滑动更新质心）

    def __init__(self, threshold: float = 0.72):
        self.threshold = threshold
        self.speakers: list[dict] = []  # {name, centroid, count, history[]}
        self._encoder = None

    # ---------- 模型加载 ----------
    def ensure_loaded(self, model_name: str) -> None:
        self._encoder = get_encoder(model_name)

    @property
    def is_ready(self) -> bool:
        """编码器是否已加载（未加载时避免阻塞处理线程）。"""
        return _ENCODER is not None

    # ---------- 嵌入 ----------
    def embed(self, audio: np.ndarray) -> np.ndarray:
        import torch
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        sig = torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0)
        with torch.no_grad():
            emb = self._encoder.encode_batch(sig).squeeze(1)[0].cpu().numpy()
        n = float(np.linalg.norm(emb))
        return emb / n if n > 1e-9 else emb

    # ---------- 识别 ----------
    def identify(self, audio: np.ndarray, model_name: str,
                 prev_speaker: str | None = None,
                 temporal_threshold: float | None = None) -> tuple[str, int]:
        """返回 (发言人名称, 发言人序号索引，用于 GUI 配色)。

        temporal_threshold: 与上一发言人的相似度超过此阈值时直接沿用，
        减少 VAD 把一句话切成多段后声纹碎片。
        """
        self.ensure_loaded(model_name)
        emb = self.embed(audio)
        if not self.speakers:
            self._add("发言人1", emb)
            return "发言人1", 0

        # --- 1. 时序平滑：优先沿用上一发言人（减少碎片） ---
        if prev_speaker and temporal_threshold is not None:
            prev_sp = next((sp for sp in self.speakers if sp["name"] == prev_speaker), None)
            if prev_sp is not None:
                sim = self._max_sim(emb, prev_sp)
                if sim >= temporal_threshold:
                    self._update(prev_sp, emb)
                    return prev_sp["name"], self.speakers.index(prev_sp)

        # --- 2. 标准匹配：与所有已知发言人的历史嵌入取最大相似度 ---
        best_i, best_sim = -1, -1.0
        for i, sp in enumerate(self.speakers):
            sim = self._max_sim(emb, sp)
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_sim >= self.threshold:
            sp = self.speakers[best_i]
            self._update(sp, emb)
            return sp["name"], best_i
        name = f"发言人{len(self.speakers) + 1}"
        self._add(name, emb)
        return name, len(self.speakers) - 1

    # ---------- 内部 ----------
    @staticmethod
    def _max_sim(emb: np.ndarray, sp: dict) -> float:
        """与发言人全部历史嵌入的最大余弦相似度。

        只和质心比一个值，短段嵌入抖动会把同一个人拆开；只要与历任
        任一条历史嵌入足够像就归并，稳定得多。
        """
        best = -1.0
        for h in sp.get("history", (sp["centroid"],)):
            s = float(np.dot(emb, h))
            if s > best:
                best = s
        return best

    def _update(self, sp: dict, emb: np.ndarray) -> None:
        sp["count"] += 1
        hist = sp.setdefault("history", [sp["centroid"]])
        hist.append(emb)
        if len(hist) > self.MAX_HIST:
            hist.pop(0)
        # 质心 = 历史嵌入均值（滑动更平滑），再归一化
        c = np.mean(hist, axis=0)
        n = float(np.linalg.norm(c))
        sp["centroid"] = c / n if n > 1e-9 else c

    def _add(self, name: str, emb: np.ndarray) -> None:
        self.speakers.append({
            "name": name, "centroid": emb.copy(), "count": 1,
            "history": [emb.copy()],
        })

    @property
    def speaker_names(self) -> list[str]:
        return [sp["name"] for sp in self.speakers]
