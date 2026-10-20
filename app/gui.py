# -*- coding: utf-8 -*-
"""GUI：录音控制 / 实时分声轨转写 / 输入电平 / 结束会议 / AI 纪要进度。"""
from __future__ import annotations

import math
import queue
import subprocess
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk, messagebox

from . import store
from .pipeline import MeetingSession, SPEAKER_COLORS

# ---------- 设计体系（浅色，clam 主题） ----------
BG        = "#f4f5f8"   # 窗口背景
CARD      = "#ffffff"   # 卡片/面板
BORDER    = "#e3e6ec"
TEXT      = "#1f2430"
SUB       = "#7a8291"
ACCENT    = "#2f6fed"   # 主色（聚焦/进度）
GREEN     = "#30a46c"   # 成功（开始录音）
RED       = "#e5484d"   # 危险（结束会议）
AMBER     = "#e67e22"
GRAY_BTN  = "#f1f3f7"
FONT      = "PingFang SC"
MONO      = "Menlo"
LEVEL_BG  = "#eef0f4"
MUTE_SECS_WARN = 3.0    # 连续无输入多少秒后红字警告
WEAK_RMS = 0.012        # 输入电平低于该 rms（约 -38 dBFS）视为"音量过弱"，转写不可靠
WEAK_SECS_WARN = 5.0    # 连续弱音量多少秒后提示"离麦克风太远/输入音量过小"


def _build_style(root: tk.Tk) -> ttk.Style:
    """浅色视觉体系；按钮使用系统原生样式（macOS aqua），保证任何环境可见。"""
    style = ttk.Style(root)
    # 不强制切换主题：保持系统主题，自定义仅作用于 Frame/Label/Entry 配色
    style.configure("Card.TFrame", background=CARD)
    style.configure("TFrame", background=BG)
    style.configure("H1.TLabel", background=BG, foreground=TEXT,
                    font=(FONT, 15, "bold"))
    style.configure("Sub.TLabel", background=BG, foreground=SUB, font=(FONT, 11))
    style.configure("CardH1.TLabel", background=CARD, foreground=TEXT,
                    font=(FONT, 13, "bold"))
    style.configure("Muted.TLabel", background=BG, foreground=SUB)
    style.configure("Status.TLabel", background=BG, foreground="#3d4452")
    style.configure("Timer.TLabel", background=BG, foreground=RED,
                    font=(MONO, 15, "bold"))
    style.configure("Stage.TLabel", background=CARD, foreground=SUB, font=(FONT, 11))
    style.configure("Warn.TLabel", background=BG, foreground=RED,
                    font=(FONT, 11, "bold"))
    style.configure("TProgressbar", thickness=8)
    return style


class RecorderApp:
    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.style = _build_style(root)
        self.ui_q: "queue.Queue[tuple]" = queue.Queue()
        self.session: MeetingSession | None = None
        self.recording = False
        self.finalizing = False
        self.waiting_ai = False
        self.start_wall = 0.0
        self.minutes_path: str | None = None
        self.dev_index_map: dict[str, int] = {}
        self._last_status_state = None
        self._mute_secs = 0.0
        # 显示模型：全量重建转写区，避免 Tk index/mark 锚点在增量删除时
        # 漂移造成的"累积重复"（2026-10-19 根因修复，见 replays/display_layer.md）
        self._display_items: list[dict] = []  # {"kind": "utt", "utt": u} | {"kind": "msg", "tag": t, "text": s}
        self._build()
        self._center_window()
        self.root.after(100, self._poll)

    # ================= 界面构建 =================
    def _center_window(self) -> None:
        """按屏幕自适应尺寸并居中，确保底部按钮完整可见。"""
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = max(760, min(920, sw - 60))
        h = max(540, min(740, sh - 140))
        self.root.minsize(760, 540)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)  # 略偏上，避免遮挡 Dock
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self) -> None:
        r = self.root
        r.title("WorkBuddy 会议录音 · 实时转写与 AI 纪要")
        r.configure(bg=BG)

        # ---- 顶部标题 ----
        head = ttk.Frame(r, padding=(20, 14, 20, 4))
        head.pack(fill="x")
        ttk.Label(head, text="会议录音 · 实时转写与 AI 纪要",
                  style="H1.TLabel").pack(side="left")
        ttk.Label(head, text="声纹分人 · 自动生成纪要",
                  style="Sub.TLabel").pack(side="right", pady=(6, 0))

        # ---- 输入卡片：主题 + 设备 ----
        card1 = ttk.Frame(r, style="Card.TFrame", padding=(18, 14))
        card1.pack(fill="x", padx=20, pady=(10, 6))
        ttk.Label(card1, text="会议主题", style="CardH1.TLabel").pack(anchor="w")
        self.title_var = tk.StringVar(value="")
        ttk.Entry(card1, textvariable=self.title_var, width=30).pack(
            side="left", padx=(0, 18), fill="x", expand=True, pady=(8, 0))
        self.dev_var = tk.StringVar()
        self._setup_devices()
        devbox = ttk.Combobox(card1, textvariable=self.dev_var, width=30,
                              state="readonly",
                              values=list(self.dev_index_map.keys()) or ["系统默认输入"])
        devbox.pack(side="right", padx=(0, 0), pady=(8, 0))
        ttk.Label(card1, text="输入设备", style="Muted.TLabel").pack(side="right", padx=(0, 8))

        # ---- 状态条 + 计时 ----
        bar = ttk.Frame(r, padding=(22, 2))
        bar.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪 — 点击「开始录音」")
        ttk.Label(bar, textvariable=self.status_var,
                  style="Status.TLabel").pack(side="left")
        self.timer_var = tk.StringVar(value="00:00:00")
        ttk.Label(bar, textvariable=self.timer_var,
                  style="Timer.TLabel").pack(side="right")

        # ---- 电平条卡片 ----
        lvl = ttk.Frame(r, style="Card.TFrame", padding=(18, 10))
        lvl.pack(fill="x", padx=20, pady=(4, 6))
        ttk.Label(lvl, text="输入电平", style="CardH1.TLabel").pack(side="left")
        self.level_cv = tk.Canvas(lvl, width=330, height=22, bg=LEVEL_BG,
                                  highlightthickness=1, highlightbackground=BORDER)
        self.level_cv.pack(side="left", padx=(12, 10), fill="x", expand=True)
        self.level_db_var = tk.StringVar(value="-∞ dB")
        ttk.Label(lvl, textvariable=self.level_db_var, foreground=SUB,
                  font=(MONO, 12, "bold"), background=CARD).pack(side="left")
        ttk.Label(lvl, text="说话时电平跳动 = 正在录音",
                  style="Muted.TLabel", background=CARD).pack(side="left", padx=(14, 0))
        self.level_warn_var = tk.StringVar(value="")
        ttk.Label(lvl, textvariable=self.level_warn_var, foreground=RED,
                  font=(FONT, 11, "bold"), background=CARD).pack(side="left", padx=(16, 0))
        self.btn_mic = ttk.Button(lvl, text="麦克风设置", width=10,
                                  command=self.open_mic_settings)
        self.btn_mic.pack(side="right")

        # ---- 转写正文（先创建不 pack；底部按钮条 pack 后再 expand 占剩余） ----
        body = ttk.Frame(r, style="Card.TFrame", padding=(2, 2))
        self.text = tk.Text(body, wrap="word", state="disabled",
                            bg=CARD, fg=TEXT, bd=0, height=8,
                            font=(FONT, 13), padx=16, pady=12,
                            spacing1=2, spacing3=6, insertbackground=ACCENT)
        sb = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for i, c in enumerate(SPEAKER_COLORS):
            self.text.tag_configure(f"sp{i}", foreground=c,
                                    font=(FONT, 13, "bold"))
        self.text.tag_configure("ts", foreground="#a5adbb", font=(MONO, 10))
        self.text.tag_configure("sys", foreground="#2f6fed", font=(FONT, 12))
        self.text.tag_configure("err", foreground=RED, font=(FONT, 12, "bold"))
        self._append("sys", "使用说明：点击「开始录音」→ 说话时上方电平条跳动、正文实时显示分声轨转写"
                            "→ 会议结束点击「结束会议」→ 自动保存转写并调用配置的模型接口生成纪要保存到 Obsidian。\n")
        try:
            from . import summarize as _sum
            if not _sum.is_configured():
                self._append("sys", "提示：尚未配置 AI 纪要接口，点击「设置」可两步完成"
                                    "（可选；不配置也能正常录音转写）。\n")
        except Exception:  # noqa: BLE001
            pass

        # ---- 底部操作条：side="bottom" 优先锁定，防止被转写区挤出窗口 ----
        foot = ttk.Frame(r, style="Card.TFrame", padding=(18, 12))
        foot.pack(side="bottom", fill="x", padx=20, pady=(0, 16))
        self.btn_start = ttk.Button(foot, text="▶  开始录音", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_end = ttk.Button(foot, text="■  结束会议（生成纪要）", command=self.end,
                                  state="disabled")
        self.btn_end.pack(side="left", padx=10)
        self.btn_open = ttk.Button(foot, text="打开纪要文件夹", command=self.open_folder,
                                   state="disabled")
        self.btn_open.pack(side="left")
        self.btn_settings = ttk.Button(foot, text="设置", width=8,
                                       command=self.open_settings)
        self.btn_settings.pack(side="left", padx=(10, 0))

        self.progress = ttk.Progressbar(foot, mode="determinate", maximum=100,
                                        value=0, length=240)
        self.progress.pack(side="right", padx=(12, 8))
        self.stage_var = tk.StringVar(value="")
        ttk.Label(foot, textvariable=self.stage_var, style="Stage.TLabel",
                  background=CARD).pack(side="right")

        # ---- 最后 pack 转写区（占剩余全部空间） ----
        body.pack(fill="both", expand=True, padx=20, pady=(2, 8))

        # 初始绘制一次灰电平条（未录音也有 UI）
        self._draw_level(0.0)

    def _setup_devices(self) -> None:
        """枚举输入设备（统一用 sounddevice，与采集后端索引一致）。

        2026-10-19 修复：
        1. 只用 sounddevice 枚举——pyaudio 与 sounddevice 的设备索引可能
           错位（GUI 选序号 i 时 recorder 按 sounddevice 索引打开，可能
           打开到 iPhone 麦克风），统一枚举源从根上消除错位；
        2. 过滤 iPhone 连续互通麦克风（录到的信号窄带低频、Whisper 无
           法识别，实测能量集中在 100-300Hz、1k-4kHz≈0）；
        3. 默认选中 MacBook 内置麦克风而非「系统默认输入」。
        """
        self.dev_index_map = {}
        try:
            import sounddevice as sd
            for i, info in enumerate(sd.query_devices()):
                if info.get("max_input_channels", 0) > 0:
                    self.dev_index_map[f"{i}: {info['name']}"] = i
        except Exception as e:  # noqa: BLE001
            try:
                import pyaudio
                p = pyaudio.PyAudio()
                try:
                    for i in range(p.get_device_count()):
                        info = p.get_device_info_by_index(i)
                        if info.get("maxInputChannels", 0) > 0:
                            self.dev_index_map[f"{i}: {info['name']}"] = i
                finally:
                    p.terminate()
            except Exception as e2:  # noqa: BLE001
                self.status_var.set(f"麦克风枚举失败：{e} / {e2}")
        if not self.dev_index_map:
            self.dev_index_map = {}
        # ---- 过滤非本机输入（iPhone / AirPods / 蓝牙等"某某的麦克风"）----
        # iPhone 连续互通麦克风录到的是窄带低频信号，Whisper 无法转写
        # （2026-10-19 实测：能量集中在 100-300Hz，1k-4kHz≈0）。
        mac_devices = {}
        for label, idx in self.dev_index_map.items():
            name = label.split(":", 1)[-1] if ":" in label else label
            if not name:
                continue
            low = name.lower()
            if "iphone" in low or "的麦克风" in name or "airpods" in low \
               or "bluetooth" in low or "蓝牙" in name:
                continue
            mac_devices[label] = idx
        if mac_devices:
            self.dev_index_map = mac_devices
        # 默认选中 MacBook 内置麦克风（存在时），否则第一个设备；
        # 不再默认「系统默认输入」，避免又指向 iPhone。
        prefer = next((l for l in self.dev_index_map
                       if "macbook" in l.lower() or "内置麦克风" in l or "built-in" in l.lower()
                       or "microphone" in l.lower() or "麦克风" in l),
                      next(iter(self.dev_index_map)) if self.dev_index_map else "")
        self.dev_var.set(prefer)

    # ================= 录音 =================
    def start(self) -> None:
        if self.recording:
            return
        title = self.title_var.get().strip()
        label = self.dev_var.get()
        device = self.dev_index_map.get(label)
        if device is not None and device < 0:
            device = None
        try:
            self.session = MeetingSession(self.cfg, title=title, device=device, ui_q=self.ui_q)
            self.session.start()
        except Exception as e:  # noqa: BLE001
            if self.session is not None:  # 释放可能已打开的音频流
                try:
                    self.session.rec.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._show_error("无法开始录音", e)
            return
        self.recording = True
        self.start_wall = time.time()
        self._mute_secs = 0.0
        self._weak_secs = 0.0
        self.btn_start.config(text="●  录音中…", state="disabled")
        self.btn_end.config(state="normal")
        self.btn_open.config(state="disabled")
        self.progress.config(value=5)
        self.status_var.set("● 录音中 — 说话时电平条应跳动（声纹自动区分发言人）")
        self.level_warn_var.set("")
        self._tick()

    def end(self) -> None:
        if not self.recording or self.finalizing:
            return
        self.finalizing = True
        self.btn_end.config(state="disabled")
        self.status_var.set("正在保存录音与转写…")
        self.progress.config(value=10)
        self.stage_var.set("保存转写中")
        threading.Thread(target=self._finalize, daemon=True).start()

    def _finalize(self) -> None:
        try:
            res = self.session.end()
            meta = res["meta"]
            self.ui_q.put(("saved", meta))
            # 一键自动生成：调用 .env 配置的接口（无需回到 WorkBuddy 对话）
            from . import summarize
            ok, msg, _paths = summarize.generate_minutes(self.cfg, meta, res["utterances"])
            if not ok:
                self.ui_q.put(("ai_blocked", msg))
        except Exception:  # noqa: BLE001
            self.ui_q.put(("error", traceback.format_exc()))

    # ================= 定时轮询 =================
    def _poll(self) -> None:
        changed = False
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                self._handle(kind, payload)
                changed = True
        except queue.Empty:
            pass
        if changed:
            self._redraw()  # 模型有更新则统一全量重绘（避免重绘开销）
        if self.recording and not self.finalizing:
            self._tick()
            self._refresh_level()
        elif not self.recording:
            self._refresh_level()  # 未录音时也维持电平条 UI（灰条 + -∞）
        if self.waiting_ai:
            self._poll_ai_status()
        self.root.after(150, self._poll)

    def _handle(self, kind: str, payload) -> None:
        if kind == "utt":
            self._append_utt(payload)
        elif kind == "utt_merge":
            self._merge_last_utt(payload)
        elif kind == "recording":
            self.status_var.set("● 录音中 — 说话时电平条应跳动（声纹自动区分发言人）")
        elif kind == "saved":
            meta = payload
            self.recording = False
            self.finalizing = False
            self.waiting_ai = True
            self.progress.config(value=20)
            self.status_var.set(
                f"转写已保存（{int(meta['duration'] // 60)} 分钟，{len(meta['speakers'])} 位发言人），"
                f"正在自动生成纪要…无需任何操作")
            self.stage_var.set("AI 纪要生成中（自动进行，无需操作）")
            self._append("sys", "\n[转写已保存] 正在调用配置的文本模型自动生成会议纪要，"
                                "完成后本进度条会自动走满，请稍候。\n")
            self.btn_start.config(text="▶  开始新录音", state="normal")
        elif kind == "ai_blocked":
            msg = payload if isinstance(payload, str) and payload != "llm_not_configured" else None
            self.waiting_ai = False
            self.progress.config(value=15)
            self.status_var.set("转写已保存；自动生成未完成"
                                + ("" if msg is None else "（详见下方提示）"))
            self.stage_var.set("请点「设置」补全 AI 纪要配置后重新结束会议，或回对话由内置模型生成")
            detail = f"详情：{msg}\n" if msg else ""
            self._append("sys", "\n[提示] 自动生成未完成。\n"
                                f"{detail}"
                                "1) 点击「设置」填写接口地址与 API Key（公司内网证书报错时勾选"
                                "「跳过 SSL 校验」），保存后重新点「结束会议」即自动生成；\n"
                                "2) 或回到 WorkBuddy 对话发送任意消息，由对话内置模型生成纪要，本进度条同样会同步。\n")
        elif kind == "error":
            self.recording = False
            self.finalizing = False
            self.progress.config(value=0)
            self.status_var.set("处理失败")
            self.stage_var.set("失败")
            self._append("err", f"\n错误：{payload}\n")
            messagebox.showerror("出错", str(payload))
            self.btn_start.config(text="▶  开始录音", state="normal")

    def _poll_ai_status(self) -> None:
        st = store.read_status()
        if not st or st.get("state") in (None, "recording", "transcript_ready"):
            return
        if st.get("state") == "generating":
            self.status_var.set("AI 正在生成纪要…")
            self.progress.config(value=st.get("progress", 30))
            self.stage_var.set(st.get("stage", "生成中"))
        elif st.get("state") == "done":
            self.waiting_ai = False
            self.progress.config(value=100)
            self.stage_var.set("完成")
            self.minutes_path = st.get("minutes_obsidian_path")
            self.status_var.set("✔ 会议纪要已保存到 Obsidian")
            self.btn_open.config(state="normal")
            self._append("sys", f"\n[会议纪要已生成]\n保存位置：{self.minutes_path}\n"
                                f"（转写全文：{st.get('transcript_obsidian_path', '—')}）\n")
        elif st.get("state") == "error":
            self.waiting_ai = False
            self.progress.config(value=0)
            self.status_var.set("纪要生成失败")
            self.stage_var.set(st.get("error") or "失败")
            self._append("err", f"\n纪要生成失败：{st.get('error')}\n")

    def _tick(self) -> None:
        if self.recording and not self.finalizing:
            el = int(time.time() - self.start_wall)
            self.timer_var.set(f"{el // 3600:02d}:{el % 3600 // 60:02d}:{el % 60:02d}")

    # ================= 输入电平 =================
    def _refresh_level(self) -> None:
        rms = peak = 0.0
        if self.recording and self.session is not None:
            try:
                rms, peak = self.session.input_level()
            except Exception:  # noqa: BLE001
                rms = peak = 0.0
        if self.recording:
            self._mute_secs = 0.0 if rms > 1e-6 else self._mute_secs + 0.15
            # 弱音量累计：有余输入但过低（远场/小声），Whisper 转写会失败或幻觉
            if 1e-6 < rms < WEAK_RMS:
                self._weak_secs = getattr(self, "_weak_secs", 0.0) + 0.15
            else:
                self._weak_secs = 0.0
        if rms <= 1e-6:
            self.level_db_var.set("-∞ dB")
            self._draw_level(0.0)
        else:
            db = 20.0 * math.log10(rms)
            self.level_db_var.set(f"{db:+.0f} dB")
            floor, ceil = -60.0, -12.0  # dBFS 映射区间
            frac = max(0.0, min(1.0, (db - floor) / (ceil - floor)))
            self._draw_level(frac)
        if self.recording and self._mute_secs >= MUTE_SECS_WARN:
            self.level_warn_var.set(f"⚠ {int(self._mute_secs)} 秒无输入 — 点「麦克风设置」允许本应用后重新开始录音")
        elif self.recording and getattr(self, "_weak_secs", 0.0) >= WEAK_SECS_WARN:
            self.level_warn_var.set(
                "⚠ 输入音量过弱，转写会失败 — 请靠近麦克风说话，或在系统设置→声音→输入中调高「输入音量」")
        else:
            self.level_warn_var.set("")

    def _draw_level(self, frac: float) -> None:
        cv = self.level_cv
        cv.delete("all")
        w = max(cv.winfo_width(), 120)
        h = max(cv.winfo_height(), 22)
        cv.create_rectangle(0, 0, w, h, fill=LEVEL_BG, outline="")
        segs = 36
        lit = int(max(0.0, min(1.0, frac)) * segs + 0.5)
        x0, x1 = 3, w - 3
        for i in range(segs):
            sx = x0 + i * (x1 - x0) / segs
            ex = x0 + (i + 1) * (x1 - x0) / segs
            ratio = i / segs
            if ratio < 0.55:
                color = "#27ae60"     # 绿：低
            elif ratio < 0.8:
                color = AMBER          # 橙：中
            else:
                color = RED            # 红：高
            if i < lit:
                cv.create_rectangle(sx, 4, ex, h - 4, fill=color, outline="")
            else:
                cv.create_rectangle(sx, 4, ex, h - 4, fill="#e4e7ee", outline="")

    # ================= 展示（模型驱动 + 全量重绘） =================
    # 2026-10-19 修复：旧实现用 text.index("end-1c") 记录"最后一条转写起点"，
    # 再在 utt_merge 时 delete(anchor, "end") 增量替换。Tk 的 index 是静态字符串
    # 坐标，而 insert/delete 会改变行号与"哨兵换行"，实测三种写法（静态坐标 /
    # mark gravity=right / mark gravity=left）都会在连续 merge 时把删除起点弄偏，
    # 导致旧文本残留、新文本与旧文本粘连在同一行 —— 即用户看到的"累积重复"。
    # 改为维护显示模型 self._display_items，任何事件只改模型，然后整体重建一遍
    # Text（内容量小，重建开销可忽略），不存在任何锚点漂移问题。
    def _append(self, tag: str, text: str) -> None:
        """追加一条系统/错误提示（进模型，等待统一重绘）。"""
        self._display_items.append({"kind": "msg", "tag": tag, "text": text})
        self._redraw()  # 系统提示不频繁，立即重绘保证可见

    def _append_utt(self, u: dict) -> None:
        """追加一条新转写进模型。"""
        self._display_items.append({"kind": "utt", "utt": u})

    def _merge_last_utt(self, u: dict) -> None:
        """合并后的长文本替换模型里最后一条转写（原句不换行展开）。"""
        for i in range(len(self._display_items) - 1, -1, -1):
            if self._display_items[i]["kind"] == "utt":
                self._display_items[i]["utt"] = u
                return
        # 模型里没有转写（不应发生）：按新转写追加
        self._append_utt(u)

    def _redraw(self) -> None:
        """按显示模型全量重建转写区。"""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for item in self._display_items:
            if item["kind"] == "utt":
                self._insert_utt_body(item["utt"])
            else:
                self.text.insert("end", item["text"], item["tag"])
        self.text.configure(state="disabled")
        self.text.see("end")

    def _insert_utt_body(self, u: dict) -> None:
        color = u.get("_color", SPEAKER_COLORS[0])
        try:
            i = SPEAKER_COLORS.index(color)
        except ValueError:
            i = 0
        ts = time.strftime("%H:%M:%S", time.localtime(u["start"]))
        lang = u.get("lang_disp") or ""
        tag = f"sp{i}"
        self.text.insert("end", f"  {ts}  ", "ts")
        self.text.insert("end", f" {u['speaker']} ", tag)
        if lang and lang not in ("中文", ""):
            self.text.insert("end", f"[{lang}] ", "ts")
        self.text.insert("end", f"  {u['text']}\n", None)

    # ================= 工具 =================
    def open_mic_settings(self) -> None:
        """一键打开 系统设置 → 隐私与安全性 → 麦克风。"""
        try:
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("打开失败", str(e))

    def open_folder(self) -> None:
        target = self.minutes_path or self.cfg["output"].get("obsidian_dir")
        try:
            subprocess.Popen(["open", "-R", target])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("打开失败", str(e))

    def open_settings(self) -> None:
        """打开配置向导（模态子窗口）：AI 纪要（LLM）+ 纪要保存目录。"""
        from .setup_wizard import SetupWizard
        from . import summarize
        wiz = SetupWizard(self.root, self.cfg, as_toplevel=True)
        try:
            self.root.wait_window(wiz.win)
        finally:
            if wiz.done:
                d = self.cfg["output"].get("obsidian_dir", "")
                configured = summarize.is_configured()
                hint = ("AI 纪要已配置，下次「结束会议」即可自动生成。"
                        if configured
                        else "尚未配置 AI 纪要，可随时再点「设置」补填（可选）。")
                self._append("sys", f"\n[设置已保存] 纪要目录：{d}；{hint}\n")
                self.status_var.set("设置已保存" if configured
                                    else "设置已保存（未配置 AI 纪要）")

    def _show_error(self, title: str, e: Exception) -> None:
        msg = str(e)
        hint = ""
        if "NorInputStream" in msg or "Stream" in msg or "ErrInputOverflowed" in msg or "PortAudio" in msg:
            hint = ("\n\n可能原因与解决：\n"
                    "1. 尚未授权麦克风：系统设置 → 隐私与安全性 → 麦克风 → 允许（终端 / WorkBuddy）；\n"
                    "2. 没有可用的输入设备（外接麦克风未接入）。")
        messagebox.showerror(title, msg + hint)
