# -*- coding: utf-8 -*-
"""配置管理：默认值 + config.json 叠加。"""
import json
import os
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"


def _default_obsidian_dir() -> str:
    """纪要保存默认目录：用户 Documents/会议纪要（不再硬编码任何人本机路径）。

    多用户分发要求：不同电脑上该目录不存在/无权限时，_ensure_dirs 自动回退，
    程序永不因路径崩溃；后续 GUI「设置」页可自行修改保存位置。
    """
    return str(pathlib.Path.home() / "Documents" / "会议纪要")


OBSIDIAN_DIR_DEFAULT = _default_obsidian_dir()

DEFAULTS = {
    "asr": {
        "backend": "mlx",               # mlx / faster-whisper
        "mlx_model": "mlx-community/whisper-small-mlx",
        "model_size": "small",          # tiny/base/small/medium/large-v3
        "device": "cpu",                # macOS 下 ctranslate2 仅支持 cpu
        "compute_type": "int8",
        "language": "auto",             # auto = 按实际语种输出（非中文语种原样输出）
        "beam_size": 5,
        "chunk_seconds": 3,             # 实时处理分块时长（VAD 内会再切分）
        "simplify_zh": True,            # 中文统一转简体
    },
    "diarize": {
        "enabled": True,
        "threshold": 0.82,              # 声纹相似度阈值；越高越保守，减少碎片
        "temporal_threshold": 0.72,     # 与上一发言人质心相似度超此值直接沿用
        "model": "speechbrain/spkrec-ecapa-voxceleb",
        "min_speaker_segment": 1.0,     # 少于该秒数的片段不参与声纹判定（沿用上一发言人）
    },
    "vad": {
        "enabled": True,
        "min_speech": 0.5,              # 最短语音段（秒）
        "min_silence": 0.3,             # 句间最短静音（秒）
        "max_segment": 10.0,            # 单段最大长度（秒）
    },
    "audio": {
        "sample_rate": 16000,
        "channels": 1,
        "silence_rms": 0.002,           # 能量兜底 VAD 用
        "voice_peak": 0.01,
    },
    "output": {
        "obsidian_dir": OBSIDIAN_DIR_DEFAULT,
        "save_wav": True,               # 是否保留原始录音
    },
    "hf_endpoint": "https://hf-mirror.com",  # 模型下载镜像；大陆网络必需
    "download": {
        "source": "auto",               # 模型下载源: auto(双源测速择优) / hf_mirror / modelscope
    },
    "llm": {                            # 备用：仅供本地调试，正式纪要由 WorkBuddy 内置模型生成
        "provider": "auto",             # auto / ollama / openai / builtin
        "ollama_url": "http://localhost:11434",
        "openai_base_url": "",
        "openai_api_key": "",
        "openai_model": "",
    },
}


def load() -> dict:
    """返回合并后的配置（深合并）。"""
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            _deep_merge(cfg, user)
        except Exception as e:  # noqa: BLE001
            print(f"[config] 读取 config.json 失败，使用默认配置: {e}")
    obj = cfg.get("output", {})
    obs = (obj.get("obsidian_dir") or "").strip()
    if not obs:
        # 未设置/空值（新分发场景）→ 使用默认目录，避免保存到程序目录
        obs = OBSIDIAN_DIR_DEFAULT
        obj["obsidian_dir"] = obs
    obj["obsidian_dir"] = os.path.expanduser(obs)
    _ensure_dirs(cfg)
    return cfg


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _ensure_dirs(cfg: dict) -> None:
    for key in ("recordings", "logs"):
        d = APP_DIR / key
        d.mkdir(parents=True, exist_ok=True)
    obj = cfg.get("output", {})
    if obj:
        obs = obj.get("obsidian_dir")
        if obs:
            try:
                pathlib.Path(obs).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                # 路径不可创建（无权限/盘符不存在）→ 回退到用户 Documents/会议纪要，
                # 保证任何电脑上都能正常保存纪要 md（2026-10-20 多用户分发修复）
                print(f"[config] 纪要目录不可用，回退默认: {obs} ({e})")
                fb = _default_obsidian_dir()
                pathlib.Path(fb).mkdir(parents=True, exist_ok=True)
                obj["obsidian_dir"] = fb


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
