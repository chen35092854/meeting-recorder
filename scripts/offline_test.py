# -*- coding: utf-8 -*-
"""离线回测：用最近一次真实录音跑完整管线（VAD -> ASR -> 分人），
验证"开始录音、1234"能被正确识别为中文、且 speaker 数接近 1。"""
import io
import json
import logging
import sys
import time

import numpy as np

sys.path.insert(0, "/Users/eric/WorkBuddy/2026-10-16-16-03-31/meeting-recorder")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

from app import config  # noqa: E402
from app import vad as vad_mod  # noqa: E402
from app import pipeline  # noqa: E402
from app.asr_mlx import transcribe as mlx_transcribe  # noqa: E402
from app.diarize import SpeakerRegistry  # noqa: E402

WAV = sys.argv[1] if len(sys.argv) > 1 else (
    "/Users/eric/WorkBuddy/2026-10-16-16-03-31/meeting-recorder/recordings/20261019_095333.wav"
)

cfg = config.load()
print("== 配置 ==")
print("asr.backend =", cfg["asr"]["backend"])
print("asr.language =", cfg["asr"]["language"])
print("vad =", json.dumps(cfg["vad"], ensure_ascii=False))
print("diarize =", json.dumps(cfg["diarize"], ensure_ascii=False))

# --- 读 wav ---
import wave
with wave.open(WAV, "rb") as w:
    sr = w.getframerate()
    ch = w.getnchannels()
    n = w.getnframes()
    raw = w.readframes(n)
audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
if ch > 1:
    audio = audio.reshape(-1, ch).mean(axis=1)
print(f"\n== 音频: {n/sr:.1f}s  sr={sr} ch={ch} ==")

# --- VAD 切段 ---
t0 = time.time()
segs = vad_mod.split_speech(
    audio, sr,
    min_speech=cfg["vad"].get("min_speech", 0.5),
    min_silence=cfg["vad"].get("min_silence", 0.5),
    max_segment=cfg["vad"].get("max_segment", 10.0),
    silero_threshold=cfg["vad"].get("silero_threshold", 0.6),
)
print(f"\n== VAD: {len(segs)} 个语音段 ({time.time()-t0:.2f}s) ==")
for i, (s, e) in enumerate(segs[:25]):
    print(f"  段{i}: {s/sr:6.2f}s ~ {e/sr:6.2f}s  时长={(e-s)/sr:.2f}s")

# --- 逐段 ASR + 分人 ---
print("\n== 转写 + 分人 ==")
reg = SpeakerRegistry(threshold=cfg["diarize"]["threshold"])
prev = "发言人1"
total_segs = 0
t_start = time.time()
for i, (s, e) in enumerate(segs[:30]):
    seg_audio = audio[s:e]
    seg_dur = (e - s) / sr
    t0 = time.time()
    try:
        utts = mlx_transcribe(cfg, seg_audio, sr)
    except Exception as ex:
        print(f"  段{i} 识别失败: {ex}")
        continue
    dt = time.time() - t0
    total_segs += 1
    # 段级统一分人
    if reg.is_ready or True:
        try:
            from app.diarize import get_encoder
            if not hasattr(reg, "_encoder") or reg._encoder is None:
                get_encoder(cfg["diarize"]["model"])
        except Exception:
            pass
    speaker, color = reg.identify(
        seg_audio, cfg["diarize"]["model"],
        prev_speaker=prev,
        temporal_threshold=cfg["diarize"].get("temporal_threshold", 0.62),
    )
    prev = speaker
    text = " / ".join(t for _, _, t, _ in utts) or "(空)"
    langs = set(l for _, _, _, l in utts)
    print(f"  段{i} [{s/sr:6.2f}s {seg_dur:.2f}s] {speaker} 语言={langs} ({dt*1000:.0f}ms)")
    for st, en, t, lg in utts:
        print(f"      [{st:5.2f}s~{en:5.2f}s] {t}")

print(f"\n== 汇总: {total_segs} 段已识别, 发言人 = {reg.speaker_names} 共用时 {time.time()-t_start:.1f}s ==")
