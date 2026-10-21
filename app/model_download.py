# -*- coding: utf-8 -*-
"""模型下载统一管理：就绪检测 + 带进度下载（国内镜像 + 禁用 Xet）。

为什么必须禁用 Xet（2026-10-21 同事 v1.0.10 实测，重要教训）：
新版 huggingface_hub 默认使用 Xet 存储后端下载大文件，Xet 的 CAS 服务器
（cas-server.xethub.hf.co）不在 hf-mirror.com 反向代理覆盖范围内 ——
镜像 + Xet 必然 401 Unauthorized：

    Task error: File reconstruction error: CAS Client Error:
    Request error: HTTP status client error (401 Unauthorized),
    domain: https://cas-server.xethub.hf.co/v2/reconstructions/...

表现为「ASR 预热失败 → 转写区永远不出字」且无任何用户可见提示。
解决方法：HF_HUB_DISABLE_XET=1 强制回退普通 HTTP 下载，镜像随即生效。

本模块是全部模型下载的唯一入口，统一保证：
    HF_ENDPOINT                 = 镜像（config.hf_endpoint，默认 hf-mirror.com）
    HF_HUB_DISABLE_XET          = 1（关键，否则镜像下大文件 401）
    HF_HUB_DISABLE_PROGRESS_BARS = 1（禁用 tqdm，进度由 GUI 自绘）
"""
from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"


# ---------- 后端固化 ----------

def resolve_backend(cfg: dict) -> dict:
    """非 Apple Silicon（MLX 不可用）环境自动切 faster-whisper 并写回配置。

    防坑（v1.0.11）：配置默认 backend=mlx，而 Intel Mac 上 mlx 装不上——
    若启动时不固化，会先下载 MLX 模型（数百 MB），首次转写失败后才回退
    faster-whisper 再下载一套（共 ~1GB 白下）。此处一并写回 config.json，
    后续启动直接走 faster-whisper。
    """
    if cfg["asr"].get("backend", "mlx") != "mlx":
        return cfg
    try:
        import platform
        if platform.machine() != "arm64":
            raise ImportError("非 Apple Silicon，MLX 不可用")
        import mlx_whisper  # noqa: F401
        return cfg
    except Exception:  # noqa: BLE001
        cfg["asr"]["backend"] = "faster-whisper"
        logger.warning("当前环境不支持 MLX，已自动切换为 faster-whisper 后端")
        try:
            from . import config as cfg_mod
            data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
            data.setdefault("asr", {})["backend"] = "faster-whisper"
            cfg_mod.CONFIG_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:  # noqa: BLE001 写回失败不影响本次运行
            pass
        return cfg


# ---------- 环境准备 ----------

def setup_env(cfg: dict | None = None) -> None:
    """确保 huggingface_hub 走国内镜像且禁用 Xet（幂等，可重复调用）。"""
    hf = (cfg or {}).get("hf_endpoint") or DEFAULT_HF_ENDPOINT
    os.environ.setdefault("HF_ENDPOINT", hf)
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"


# ---------- 仓库定位 ----------

def whisper_repo(cfg: dict) -> str:
    """返回当前 ASR 后端对应的 HuggingFace 仓库。"""
    backend = cfg["asr"].get("backend", "mlx")
    if backend == "mlx":
        return str(cfg["asr"].get("mlx_model", "mlx-community/whisper-small-mlx"))
    size = str(cfg["asr"].get("model_size", "small"))
    return f"Systran/faster-whisper-{size}"


def spk_repo(cfg: dict) -> str:
    return str(cfg.get("diarize", {}).get(
        "model", "speechbrain/spkrec-ecapa-voxceleb"))


# ---------- 就绪检测 ----------

def _snapshot_exists(repo: str) -> bool:
    """仓库是否已完整缓存在本地（local_files_only，不联网）。"""
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=repo, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001 缓存不完整/不存在
        return False


def asr_ready(cfg: dict) -> bool:
    # 人工下载优先：本地模型目录已有主权重即视为就绪
    if local_asr_dir(cfg):
        return True
    return _snapshot_exists(whisper_repo(cfg))


def spk_ready(cfg: dict) -> bool:
    if not cfg.get("diarize", {}).get("enabled", True):
        return True
    return _snapshot_exists(spk_repo(cfg))


def missing(cfg: dict) -> list[str]:
    """当前缺失（未就绪）的模型显示名列表。"""
    out: list[str] = []
    if not asr_ready(cfg):
        out.append("语音识别模型(Whisper)")
    if not spk_ready(cfg):
        out.append("声纹模型(Speaker)")
    return out


def ready(cfg: dict) -> bool:
    return not missing(cfg)


# ---------- 人工下载通道（v1.0.11：多次失败后的兜底） ----------
# 自动下载连续失败 >= 2 次后，GUI 展示直链并允许用户用浏览器/下载工具
# 手工下载「主权重文件」，再通过「导入」按钮把文件放到本地模型目录，
# 程序随后自动补齐其余小文件（或在重试时断点续传）。

LOCAL_WHISPER_DIR = os.path.join(os.path.expanduser("~"), ".cache",
                                 "workbuddy", "whisper-local")


def main_file_name(cfg: dict) -> str:
    """当前 ASR 后端的主权重文件名（人工下载只需这一个最大的文件）。"""
    if cfg["asr"].get("backend", "mlx") == "mlx":
        return "weights.safetensors"
    return "model.bin"


def manual_urls(cfg: dict) -> list[tuple[str, str]]:
    """人工下载直链列表 [(标签, URL), ...]（镜像优先，官网备用）。"""
    repo = whisper_repo(cfg)
    fn = main_file_name(cfg)
    hf = os.environ.get("HF_ENDPOINT") or DEFAULT_HF_ENDPOINT
    return [
        ("国内镜像直链（优先）",
         f"{hf}/{repo}/resolve/main/{fn}"),
        ("官网直链（备选）",
         f"https://huggingface.co/{repo}/resolve/main/{fn}"),
    ]


def local_asr_dir(cfg: dict) -> str | None:
    """返回人工下载的本地模型目录（主权重已就位），否则 None。"""
    d = LOCAL_WHISPER_DIR
    if os.path.isdir(d) and os.path.isfile(os.path.join(d, main_file_name(cfg))):
        return d
    return None


def import_main_file(cfg: dict, src_path: str) -> tuple[bool, str]:
    """把用户手动下载的主权重文件复制到本地模型目录。

    调用方应在后台线程执行（文件可达数百 MB）。返回 (是否成功, 提示)。
    """
    src = os.path.abspath(os.path.expanduser(src_path))
    if not os.path.isfile(src):
        return False, f"文件不存在：{src}"
    want = main_file_name(cfg)
    if os.path.basename(src) != want:
        return False, (f"文件名不匹配：当前语音后端需要「{want}」，\n"
                       f"你选择的是「{os.path.basename(src)}」。"
                       f"请在浏览器中下载名为 {want} 的文件后重新导入。")
    size = os.path.getsize(src)
    if size < 50 * 1024 * 1024:
        return False, (f"文件过小（{size / 1e6:.1f} MB），可能不是完整模型，"
                       f"请重新下载（完整约数百 MB）。")
    import shutil
    os.makedirs(LOCAL_WHISPER_DIR, exist_ok=True)
    dest = os.path.join(LOCAL_WHISPER_DIR, want)
    if os.path.abspath(dest) == src:
        return True, "文件已在本地模型目录中。"
    shutil.copy2(src, dest)
    return True, (f"已导入 {want}（{size / 1e6:.1f} MB），"
                  f"正在自动补齐其余小文件…")


def fill_local_small(cfg: dict, progress_cb=None) -> None:
    """把仓库内除主权重外的其余小文件补齐到本地模型目录（联网，几 MB）。"""
    local_dir = local_asr_dir(cfg)
    if local_dir is None:
        return
    repo = whisper_repo(cfg)
    main = main_file_name(cfg)
    try:
        from huggingface_hub import hf_hub_download, list_repo_files
        files = list_repo_files(repo)
        total = len(files)
        for i, fn in enumerate(files):
            if fn == main or fn.endswith("/"):
                continue
            hf_hub_download(repo_id=repo, filename=fn, local_dir=local_dir)
            if progress_cb is not None:
                progress_cb(fn, i + 1, total)
    except Exception as e:  # noqa: BLE001 小文件缺失时由运行时加载报错兜底
        logger.warning("补齐小文件失败（不影响主权重使用）: %s", e)


# ---------- 进度状态（GUI 轮询读取） ----------

class DownloadState:
    """全局单例：模型下载进度状态，向导/主界面轮询展示。"""
    _inst: "DownloadState | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.state = "idle"        # idle / running / done / error
        self.phase = ""            # 当前下载的模型显示名
        self.file_name = ""        # 当前文件
        self.completed = 0
        self.total = 0
        self.percent = 0.0
        self.error = ""
        self.missing: list[str] = []
        self.fail_count = 0        # 自动下载连续失败次数（>=2 显示人工通道）

    @classmethod
    def get(cls) -> "DownloadState":
        with cls._lock:
            if cls._inst is None:
                cls._inst = cls()
            return cls._inst

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "phase": self.phase,
                "file_name": self.file_name,
                "completed": self.completed,
                "total": self.total,
                "percent": self.percent,
                "error": self.error,
                "missing": list(self.missing),
                "fail_count": self.fail_count,
            }

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            # 只要更新了字节数就自动重算百分比（防止调用方漏传 percent）
            if "completed" in kw or "total" in kw:
                self.percent = (self.completed / self.total * 100
                                if self.total else 0.0)


def fmt_bytes(n: int | float) -> str:
    n = max(0, int(n or 0))
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"


# ---------- 下载 ----------

def _download_snapshot(repo: str, state: DownloadState | None = None,
                       progress_cb=None) -> None:
    """下载单个 HF 仓库到标准缓存；支持断点续传与进度回调。"""
    setup_env()
    from huggingface_hub import snapshot_download

    def _cb(progress) -> None:
        file_name = str(getattr(progress, "file_name", "") or "")
        done = int(getattr(progress, "completed", 0) or 0)
        total = int(getattr(progress, "total", 0) or 0)
        if state is not None:
            state._set(file_name=file_name, completed=done, total=total,
                       percent=(done / total * 100 if total else 0.0))
        if progress_cb is not None:
            progress_cb(file_name, done, total)

    try:
        snapshot_download(repo_id=repo, resume_download=True, callback=_cb)
    except TypeError:
        # 老版本 huggingface_hub 不支持 callback 参数 → 无进度下载兜底
        logger.warning("huggingface_hub 不支持进度回调（版本过老），"
                       "回退无进度下载: %s", repo)
        snapshot_download(repo_id=repo, resume_download=True)


def ensure_missing(cfg: dict, state: DownloadState | None = None,
                   progress_cb=None) -> bool:
    """按需下载缺失模型（语音识别 → 声纹），全部就绪返回 True。

    - 语音识别优先走「人工下载的本地目录」（若存在），主权重已就位则
      只补齐其余小文件；
    - 声纹模型失败不阻断录音（降级为单人转写）；
    - 语音识别模型自动下载失败返回 False，并累计 fail_count（>=2 时
      GUI 展示人工下载通道）。
    """
    setup_env(cfg)
    ok = True

    if local_asr_dir(cfg) is None:
        repo_asr = whisper_repo(cfg)
        if not _snapshot_exists(repo_asr):
            if state is not None:
                state._set(state="running", phase="语音识别模型(Whisper)",
                           file_name="", completed=0, total=0, percent=0.0,
                           error="")
            try:
                _download_snapshot(repo_asr, state, progress_cb)
            except Exception as e:  # noqa: BLE001
                ok = False
                logger.warning("语音识别模型下载失败: %s", e)
                if state is not None:
                    state.fail_count += 1
                    state._set(state="error", error=str(e),
                               missing=missing(cfg))
                return False
    else:
        # 人工已下主权重：app 启动/向导尽早把小文件补齐（缺失时运行时再报）
        try:
            fill_local_small(cfg, progress_cb)
        except Exception as e:  # noqa: BLE001
            logger.warning("补齐本地模型小文件失败: %s", e)

    if cfg.get("diarize", {}).get("enabled", True):
        repo_spk = spk_repo(cfg)
        if not _snapshot_exists(repo_spk):
            if state is not None:
                state._set(state="running", phase="声纹模型(Speaker)",
                           file_name="", completed=0, total=0, percent=0.0)
            try:
                _download_snapshot(repo_spk, state, progress_cb)
            except Exception as e:  # noqa: BLE001
                logger.warning("声纹模型下载失败（可降级单人转写）: %s", e)

    if state is not None:
        state.fail_count = 0  # 全部（或可降级部分）处理完成即清零
        state._set(state="done", error="", percent=100.0, missing=missing(cfg))
    return ok


def start_background(cfg: dict) -> None:
    """后台线程启动缺失模型下载（幂等：运行中/已完成且无缺失则不重复）。"""
    st = DownloadState.get()
    with st._lock:
        st.missing = missing(cfg)
        if st.state == "running":
            return
        if st.state == "done" and not st.missing:
            return
        if st.state == "done" and st.missing:
            st.state = "running"  # 之前完成过但新场景又缺（一般不会）
        if st.state == "idle":
            st.state = "running"
        st.error = ""

    def _run() -> None:
        try:
            ensure_missing(cfg, state=st)
        except Exception as e:  # noqa: BLE001
            st._set(state="error", error=str(e))

    threading.Thread(target=_run, daemon=True, name="model-download").start()
