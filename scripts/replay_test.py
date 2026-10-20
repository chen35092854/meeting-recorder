# -*- coding: utf-8 -*-
"""回放用户真实录音（新管线三层防御回归测试）。

验证目标：
1. 10:34 问题录音（非人声/幻觉）-> 其所有段应被拦截，0 发言人、0 幻觉输出；
2. 真实语音录音 -> 应正常识别且保持说话人稳定。
"""
import pathlib, os
_orig = pathlib.Path.mkdir
def _patched(self, *a, **k):
    try:
        return _orig(self, *a, **k)
    except (FileExistsError, PermissionError):
        if os.path.isdir(str(self)):
            return None
        raise
pathlib.Path.mkdir = _patched
import sys
sys.path.insert(0, '/Users/eric/WorkBuddy/2026-10-16-16-03-31/meeting-recorder')
from app import config
from app import vad as vad_mod
from app.asr_mlx import transcribe
from app.diarize import SpeakerRegistry
from app.pipeline import _zero_crossing_rate
import numpy as np, wave, logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger("replay")

cfg = config.load()
REC = '/Users/eric/WorkBuddy/2026-10-16-16-03-31/meeting-recorder/recordings'


def load_wav(path):
    with wave.open(path, 'rb') as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    return sr, np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def run_pipeline(wav_path, label):
    sr, audio = load_wav(wav_path)
    print('\n' + '=' * 70)
    print('== %s ==' % label)
    print('== %s (%.1fs) ==' % (wav_path.split('/')[-1], len(audio) / sr))
    chunk = 3 * sr
    reg = SpeakerRegistry(threshold=cfg['diarize']['threshold'])
    prev = '发言人1'
    total_segs = skipped_nonvoice = no_text = 0
    for off in range(0, len(audio), chunk):
        blk = audio[off:off + chunk]
        if not len(blk):
            break
        rms = float(np.sqrt(np.mean(blk ** 2)))
        zcr = _zero_crossing_rate(blk, sr)
        segs = vad_mod.split_speech(
            blk, sr, min_speech=cfg['vad']['min_speech'],
            min_silence=cfg['vad']['min_silence'],
            max_segment=cfg['vad']['max_segment'],
            silero_threshold=cfg['vad']['silero_threshold'],
        )
        for s, e in segs:
            total_segs += 1
            seg_audio = blk[s:e]
            seg_zcr = _zero_crossing_rate(seg_audio, sr)
            seg_dur = (e - s) / sr
            t0 = off / sr + s / sr
            # ---- 新管线步骤1：几乎无声快检（min_zcr=0.012，只拦纯低频底噪）----
            # 注意：绝不能用 0.05~0.06 拦"非人声"，远场真人声 zcr 可低至
            # 0.018~0.09（11:17 实测教训），0.06 会把真人声全部误杀。
            min_zcr = float(cfg["audio"].get("min_zcr", 0.012))
            if seg_zcr < min_zcr:
                skipped_nonvoice += 1
                print('[%5.1fs %4.2fs] zcr=%.3f < %.3f -> 跳过[几乎无声]' % (t0, seg_dur, seg_zcr, min_zcr))
                continue
            # ---- 新管线步骤2：转写 + 置信度过滤 ----
            try:
                utts = transcribe(cfg, seg_audio, sr)
            except Exception as ex:
                print('  [转写失败] %s' % ex)
                continue
            if not utts:
                no_text += 1
                print('[%5.1fs %4.2fs] zcr=%.3f 无有效文本(幻觉已过滤)' % (t0, seg_dur, seg_zcr))
                continue
            # ---- 新管线步骤3：分人 ----
            speaker, ci = reg.identify(seg_audio, cfg['diarize']['model'],
                                       prev_speaker=prev,
                                       temporal_threshold=cfg['diarize'].get('temporal_threshold', 0.62))
            prev = speaker
            tx = ' / '.join(t for _, _, t, _ in utts)
            print('[%5.1fs %4.2fs] zcr=%.3f %s: %s' % (t0, seg_dur, seg_zcr, speaker, tx[:70]))
    print('-- 结果: VAD段=%d 非人声跳过=%d 无文本=%d | 最终发言人=%s' % (
        total_segs, skipped_nonvoice, no_text, reg.speaker_names))
    return reg.speaker_names, total_segs, skipped_nonvoice, no_text


if __name__ == '__main__':
    import glob
    # 1) 10:34 问题录音：预期 0 幻觉、0 或极少发言人
    run_pipeline(os.path.join(REC, '20261019_103429.wav'), '问题录音（幻觉+7发言人）')
    # 2) 10/19 早上真实测试录音对照
    for p in sorted(glob.glob(os.path.join(REC, '20261019_09*.wav'))):
        run_pipeline(p, '真实录音对照')
