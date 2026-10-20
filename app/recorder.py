# -*- coding: utf-8 -*-
"""麦克风采集（多后端自适应）：pyaudio → sounddevice → av(avfoundation)。

优先选择自带音频库的后端，避免依赖 Homebrew 系统库：
- pyaudio 的 macOS wheel 内嵌 PortAudio，开箱即用；
- sounddevice 需要系统有 libportaudio；
- av (PyAV) 内嵌 FFmpeg，用 avfoundation 输入设备采集，作为最后兜底。

统一输出 float32 mono 16kHz，满一个分块即产出（含绝对起始时间戳），
分块同时累积到完整录音数组，供结束时保存 WAV。
"""
from __future__ import annotations

import queue
import threading
import time

import numpy as np


def detect_backend() -> str | None:
    order = ["pyaudio", "sounddevice", "av"]
    for name in order:
        try:
            __import__(name)
            return name
        except Exception:  # noqa: BLE001
            continue
    return None


class _ChunkCore:
    """分块组装核心：把 float32 mono 数据块切分为定长时间窗并投递。"""

    def __init__(self, sample_rate: int, chunk_seconds: float):
        self.sample_rate = sample_rate
        self.chunk_samples = int(chunk_seconds * sample_rate)
        self.chunks_out: "queue.Queue[tuple[float, np.ndarray]]" = queue.Queue()
        self.start_time = 0.0
        self._partial: list[np.ndarray] = []
        self._partial_len = 0
        self._acc: list[np.ndarray] = []
        self._acc_len = 0
        self._total = 0
        # ---- 输入电平（供 GUI 实时显示，最近约 0.25s）----
        self._level_n = max(1, int(0.25 * sample_rate))
        self._level_buf = np.zeros(0, dtype=np.float32)
        self.current_rms = 0.0   # float 赋值在 GIL 下原子，GUI 线程可直接读
        self.current_peak = 0.0

    def open_run(self) -> None:
        self.start_time = time.time()
        self._partial = []
        self._partial_len = 0
        self._acc = []
        self._acc_len = 0
        self._total = 0
        self._level_buf = np.zeros(0, dtype=np.float32)
        self.current_rms = 0.0
        self.current_peak = 0.0

    def feed(self, mono: np.ndarray) -> None:
        mono = np.asarray(mono, dtype=np.float32).reshape(-1)
        # ---- 电平统计：最近的输入 RMS / 峰值 ----
        if mono.size:
            if self._level_buf.size == 0:
                self._level_buf = mono.copy()
            else:
                self._level_buf = np.concatenate([self._level_buf, mono])[-self._level_n:]
            self.current_rms = float(np.sqrt(np.mean(self._level_buf ** 2)))
            self.current_peak = float(np.max(np.abs(self._level_buf), initial=0.0))
        self._partial.append(mono)
        self._partial_len += len(mono)
        if self.start_time <= 0:
            self.start_time = time.time()
        # 2026-10-19 修复时间戳虚增：chunk_offset 必须基于「本块在流中的
        # 起点样本位置」，而不是 feed 前的累计值叠加 while 内的相对值——
        # 旧写法每块多算一份偏移，utterance 间隔被虚假放大（6s 一块却
        # 记成 12s 步长），导致相邻拼合条件永远不满足、长段话被拆散。
        chunk_offset = self.start_time + self._total / self.sample_rate
        while self._partial_len >= self.chunk_samples:
            buf = np.concatenate(self._partial)
            take = buf[: self.chunk_samples]
            remaining = buf[self.chunk_samples:]
            self._partial = [remaining] if len(remaining) else []
            self._partial_len = len(remaining)
            self._acc.append(take)
            self._acc_len += len(take)
            self.chunks_out.put((chunk_offset, take))
            self._total += len(take)
            chunk_offset += self.chunk_samples / self.sample_rate

    def drain_chunk(self, timeout: float = 0.5) -> tuple[float, np.ndarray]:
        return self.chunks_out.get(timeout=timeout)

    def full_audio(self) -> np.ndarray:
        parts = list(self._acc)
        if self._partial:
            parts.append(np.concatenate(self._partial))
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts).astype(np.float32, copy=False)

    @property
    def duration(self) -> float:
        return self._acc_len / self.sample_rate


class _PyAudioRecorder(_ChunkCore):
    def __init__(self, sample_rate: int, chunk_seconds: float, device):
        super().__init__(sample_rate, chunk_seconds)
        self.device = device

    def start(self) -> None:
        import pyaudio
        self._pa = pyaudio.PyAudio()
        device = self.device if (self.device is not None and self.device >= 0) else None
        if self.device is not None and self.device < 0:
            device = None
        self.open_run()
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.sample_rate,
            input=True,
            input_device_index=device,
            frames_per_buffer=4096,
            stream_callback=self._cb,
        )
        self._stream.start_stream()

    def _cb(self, in_data, frame_count, time_info, status):  # noqa: ARG002
        arr = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
        self.feed(arr)
        return (None, 0)  # pyaudio.paContinue

    def stop(self) -> None:
        try:
            if self._stream and self._stream.is_active():
                self._stream.stop_stream()
                self._stream.close()
        finally:
            if getattr(self, "_pa", None):
                self._pa.terminate()


class _SounddeviceRecorder(_ChunkCore):
    def __init__(self, sample_rate: int, chunk_seconds: float, device):
        super().__init__(sample_rate, chunk_seconds)
        self.device = device if (device is not None and device >= 0) else None

    def start(self) -> None:
        import sounddevice as sd
        self.open_run()
        # 2026-10-19 修复采样率不匹配：
        # MacBook Pro 内置麦克风原生 44100Hz，若直接请求 16000Hz 而
        # 底层未正确重采样，音频会被按 16000 解释（实际 44100 数据），
        # zcr/频谱整体下移约 2.76 倍 → Whisper 无法识别人声（幻觉）。
        # 改为：以设备原生采样率打开，收到回调后用 scipy 明确重采样到
        # 目标 16kHz，保证频率正确。
        dev_sr = 0
        try:
            info = sd.query_devices(self.device, "input")
            dev_sr = int(round(float(info["default_samplerate"])))
        except Exception:  # noqa: BLE001
            dev_sr = 0
        self._use_sr = dev_sr if dev_sr > 0 else self.sample_rate
        self._resampler = self._make_resampler(self._use_sr, self.sample_rate)
        self._stream = sd.InputStream(
            samplerate=self._use_sr, channels=1, dtype="int16",
            device=self.device, blocksize=int(self._use_sr // 10),
            callback=self._cb,
        )
        self._stream.start()

    @staticmethod
    def _make_resampler(src, dst):
        """构造重采样器；src==dst 时返回 None（直通）。"""
        if src == dst:
            return None
        try:
            from scipy import signal as sp_signal  # noqa: F401
            return ("scipy", src, dst)
        except Exception:  # noqa: BLE001
            return None

    def _resample(self, arr: np.ndarray) -> np.ndarray:
        cfg = self._resampler
        if cfg is None or arr.size == 0:
            return arr
        from math import gcd
        from scipy import signal as sp_signal
        src, dst = cfg[1], cfg[2]
        d = gcd(src, dst)
        up, down = dst // d, src // d
        return sp_signal.resample_poly(arr, up, down).astype(np.float32, copy=False)

    def _cb(self, indata, frames, time_info, status):  # noqa: ARG002
        # int16 回调 -> float32 [-1,1]
        mono = indata[:, 0] if indata.ndim == 2 and indata.shape[1] > 1 else indata
        arr = np.asarray(mono, dtype=np.int16).astype(np.float32) / 32768.0
        res = self._resample(arr)
        self.feed(np.asarray(res, dtype=np.float32))

    def stop(self) -> None:
        if getattr(self, "_stream", None):
            self._stream.stop()
            self._stream.close()


class _AvRecorder(_ChunkCore):
    """兜底：PyAV + avfoundation 采集默认输入设备（':0'）。"""

    def __init__(self, sample_rate: int, chunk_seconds: float, device=None):
        super().__init__(sample_rate, chunk_seconds)
        self._stop = False
        self._thread = None

    def start(self) -> None:
        import av
        self.open_run()
        src = ":0" if not self.device else str(self.device)
        self._container = av.open(src, format="avfoundation")
        self._stream = next(
            (s for s in self._container.streams if s.type == "audio"), None)
        if self._stream is None:
            raise RuntimeError("avfoundation 未找到音频输入流")
        self._resampler = av.audio.resampler.AudioResampler(
            format="fltp", layout="mono", rate=self.sample_rate)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            for frame in self._container.decode(self._stream):
                if self._stop:
                    break
                for rf in self._resampler.resample(frame):
                    arr = rf.to_ndarray()  # (1, n)
                    if arr.ndim == 2:
                        arr = arr[0]
                    self.feed(np.asarray(arr, dtype=np.float32))
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop = True
        try:
            if getattr(self, "_container", None):
                self._container.close()
        except Exception:  # noqa: BLE001
            pass


def create_recorder(sample_rate: int = 16000, chunk_seconds: float = 15.0,
                    device=None, channels: int = 1) -> _ChunkCore:
    """按可用后端创建录音器；均输出 mono float32。

    2026-10-19 修复：device=None（原「系统默认输入」）时主动解析并锁定
    Mac 内置麦克风，避免落到 iPhone 连续互通麦克风——它录到的是窄带低频
    信号（频谱能量集中在 100-300Hz），Whisper 完全无法转写。
    兼容设备索引符（pyaudio/sounddevice 各自索引），保证传给后端时一致。
    """
    if device is None or device < 0:
        device = _find_mac_builtin_mic()
    backend = detect_backend()
    if backend == "pyaudio":
        return _PyAudioRecorder(sample_rate, chunk_seconds, device)
    if backend == "sounddevice":
        return _SounddeviceRecorder(sample_rate, chunk_seconds, device)
    if backend == "av":
        return _AvRecorder(sample_rate, chunk_seconds, device)
    raise RuntimeError(
        "未找到任何可用音频后端。请安装 pyaudio（pip 自带音频库）后再试。")


def _find_mac_builtin_mic() -> int | None:
    """返回 Mac 内置麦克风的设备索引（sounddevice），找不到返回 None。"""
    try:
        import sounddevice as sd
        names = sd.query_devices()
        for i, info in enumerate(names):
            if info.get("max_input_channels", 0) <= 0:
                continue
            nm = str(info.get("name", "")).lower()
            # 内置：MacBook / Built-in Microphone / 内置麦克风；
            # 排除 iPhone/AirPods/蓝牙等外接/连续互通设备
            if ("iphone" in nm or "airpods" in nm or "bluetooth" in nm
                    or "的麦克风" in nm or "appletv" in nm or "apple tv" in nm):
                continue
            if ("macbook" in nm or "built-in" in nm or "内置" in nm
                    or "内置麦克风" in nm or "microphone" in nm or "麦克风" in nm):
                return i
        # 兜底：没有明确内置名，取第一个物理输入设备
        for i, info in enumerate(names):
            if info.get("max_input_channels", 0) > 0:
                return i
    except Exception:  # noqa: BLE001
        pass
    return None
