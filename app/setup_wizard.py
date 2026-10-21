# -*- coding: utf-8 -*-
"""首次配置向导（GUI）。

触发时机：
- 首次运行：config.json 无 wizard_done 标记 → 向导直接内嵌主窗口展示；
- 主界面「设置」按钮：以模态子窗口（Toplevel）打开，随时可改。

步骤（4 步）：
1. 欢迎 & 环境检查（Python / 依赖 / 麦克风）＋ 语音识别模型下载（带进度，
   确保进入主界面即可转写；v1.0.11 新增）
2. AI 纪要（LLM）配置：写 .env（BASE_URL / API_KEY / MODEL / SSL_VERIFY），可跳过
3. 纪要保存目录：写 config.json output.obsidian_dir
4. 完成：落盘 + 写 wizard_done 标记

与 app/summarize.py 共享 .env 读取与「占位 Key」判定，保证两端行为一致。
"""
from __future__ import annotations

import json
import sys
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import config as cfg_mod
from . import summarize

# 与 gui.py 一致的设计体系（浅色）
BG, CARD, BORDER = "#f4f5f8", "#ffffff", "#e3e6ec"
TEXT, SUB, ACCENT = "#1f2430", "#7a8291", "#2f6fed"
GREEN, RED, AMBER = "#30a46c", "#e5484d", "#e67e22"
FONT = "PingFang SC"
OK, BAD = "✓", "✗"

ENV_DEFAULTS = {
    "LLM_BASE_URL": "https://ai.ciic.com/llm/apis/ais-v2/chat/completions",
    "LLM_MODEL": "deepseek-v4-flash-2",
}

STEPS = ["环境检查", "AI 纪要（LLM）", "保存目录", "完成"]


def needs_first_run() -> bool:
    """config.json 中无 wizard_done=true → 首次运行，需弹向导。"""
    try:
        data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
        return not bool(data.get("wizard_done"))
    except Exception:  # noqa: BLE001
        return True


class SetupWizard:
    """4 步配置向导。

    两种模式：
    - as_toplevel=False：界面直接构建在传入的 root 上（首次运行）。
      完成/跳过时清空 root 子控件并 root.quit()，由外层接着构建主界面。
    - as_toplevel=True：构建在模态 Toplevel 上（主界面「设置」按钮）。
    """

    def __init__(self, root: tk.Misc, cfg: dict, as_toplevel: bool = False):
        self.cfg = cfg
        self.root = root
        self.done = False          # 是否完成保存
        self.win: tk.Toplevel | None = None
        self.env = summarize.load_env()
        self._page = 0
        self._pages = [self._page_welcome, self._page_llm,
                       self._page_dir, self._page_done]
        self._cards: list[tuple[tk.Frame, ...]] = []

        master: tk.Misc = root
        if as_toplevel:
            self.win = tk.Toplevel(root)
            self.win.title("设置 · 会议录音助手")
            self.win.configure(bg=BG)
            self.win.resizable(False, False)
            self.win.transient(root if isinstance(root, tk.Tk) else root.winfo_toplevel())
            self.win.protocol("WM_DELETE_WINDOW", self._on_close)
            master = self.win
        else:
            root.protocol("WM_DELETE_WINDOW", self._on_close)
            master = root
            # 修复 2026-10-20：首次运行（内嵌模式）此前未设窗口尺寸，
            # Tk 默认小窗口会被向导内容撑开、把底部按钮挤出视口
            root.resizable(False, False)

        # 两种模式统一窗口尺寸并居中（内嵌模式由主界面 _center_window 随后覆盖）
        W, H = 700, 520
        master.update_idletasks()
        sw, sh = master.winfo_screenwidth(), master.winfo_screenheight()
        master.geometry(
            f"{W}x{H}+{max(0, (sw - W) // 2)}+{max(40, (sh - H) // 3)}")

        # ---- 骨架：头部（步骤标题）/ 底部按钮（贴底锁定）/ 主体 ----
        self._head = tk.Frame(master, bg=BG)
        self._head.pack(fill="x", padx=26, pady=(20, 2))
        self._step_lbl = tk.Label(self._head, text="", bg=BG, fg=SUB,
                                  font=(FONT, 11))
        self._step_lbl.pack(anchor="w")
        self._title_lbl = tk.Label(self._head, text="", bg=BG, fg=TEXT,
                                   font=(FONT, 17, "bold"))
        self._title_lbl.pack(anchor="w", pady=(4, 0))
        # 底部操作条先锚定（side=bottom），页面内容再占剩余空间，
        # 保证任何步骤下「上一步/下一步」都可见（2026-10-20 修复）
        self._foot = tk.Frame(master, bg=BG)
        self._foot.pack(side="bottom", fill="x", padx=26, pady=(6, 22))
        self._body = tk.Frame(master, bg=BG)
        self._body.pack(fill="both", expand=True, padx=26, pady=(8, 6))

        self._go(0)

    # ================= 导航 =================

    def _go(self, idx: int) -> None:
        self._page = idx
        for c in self._body.winfo_children():
            c.destroy()
        for c in self._foot.winfo_children():
            c.destroy()
        self._cards = []
        self._pages[idx]()
        self._step_lbl.config(text=f"第 {idx + 1} 步 / 共 {len(STEPS)} 步")
        self._title_lbl.config(text=STEPS[idx])

    def _on_close(self) -> None:
        """关闭窗口 = 放弃本次修改。首次运行模式则跳过向导进入主界面。"""
        if self.win:
            try:
                self.win.grab_release()
            except Exception:  # noqa: BLE001
                pass
            self.win.destroy()
        else:
            self.dispose()
            self.root.quit()

    def dispose(self) -> None:
        """清空 root 上所有向导控件（供主界面重建）。"""
        for c in self.root.winfo_children():
            c.destroy()

    # ---------- 底部按钮 ----------

    def _footer(self, prev: bool = True, next_text: str = "下一步  >",
                on_next=None, extra=None):
        """底部按钮条；返回「下一步」按钮（供调用方按业务禁用/启用）。"""
        if extra:
            extra(self._foot)  # 左区可放「跳过此步」等
        if prev and self._page > 0:
            ttk.Button(self._foot, text="<  上一步",
                       command=lambda: self._go(self._page - 1)).pack(side="left")
        btn = ttk.Button(self._foot, text=next_text,
                         command=on_next or (lambda: self._go(self._page + 1)))
        btn.pack(side="right", padx=(10, 0))
        return btn

    def _card(self) -> tk.Frame:
        f = tk.Frame(self._body, bg=CARD, highlightthickness=1,
                     highlightbackground=BORDER)
        f.pack(fill="x", pady=(4, 6))
        self._cards.append((f,))
        return f

    def _note(self, parent: tk.Frame, text: str, color: str = SUB,
              bg: str | None = None) -> None:
        tk.Label(parent, text=text, bg=bg or parent["bg"], fg=color,
                 font=(FONT, 11), justify="left", wraplength=520
                 ).pack(anchor="w", padx=18, pady=(0, 8))

    # ================= 页面 =================
    # 各页把控件变量存入 self._v（dict），完成页读取

    # ---------- 第 1 步：环境检查 + 语音识别模型下载（v1.0.11）----------
    # 2026-10-21 同事机器修复：此前「首次开始录音时自动下载模型」且无任何
    # 进度反馈，下载又走 Xet 协议在镜像下 401 → 转写永远空白。现在在向导
    # 第 1 步就下载缺失模型并实时显示进度（MB + 百分比），确保进入主界面
    # 时模型就绪、点击「开始录音」即可看到转写。
    def _page_welcome(self) -> None:
        self._v = {}
        card = self._card()
        tk.Label(card, text="欢迎使用会议录音助手", bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w", padx=18, pady=(16, 4))
        self._note(card, "本地实时录音 + 分说话人转写 + AI 自动生成会议纪要。"
                         "所有语音只在你的电脑上处理，不离开本机。")
        for name, ok, detail in self._checks():
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", padx=18, pady=2)
            tk.Label(row, text=OK if ok else BAD, bg=CARD,
                     fg=GREEN if ok else RED, font=(FONT, 13, "bold")
                     ).pack(side="left")
            tk.Label(row, text=name, bg=CARD, fg=TEXT, font=(FONT, 11)
                     ).pack(side="left", padx=(6, 0))
            tk.Label(row, text=detail, bg=CARD, fg=SUB, font=(FONT, 10)
                     ).pack(side="right")
        self._build_model_card()
        self._next_btn = self._footer(
            prev=False, next_text="下一步  >", on_next=self._welcome_next)
        # 页面渲染完成后自动检测模型并启动下载（后台线程，不卡界面）
        self.root.after(300, self._model_tick)

    def _build_model_card(self) -> None:
        """语音识别模型下载卡片：进度条 + 状态 + 重试 / 跳过。"""
        from . import model_download as mdl
        self._mdl = mdl
        card = self._card()
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=18, pady=(14, 2))
        tk.Label(head, text="语音识别模型（首次使用需下载，数百 MB）",
                 bg=CARD, fg=TEXT, font=(FONT, 12, "bold")).pack(side="left")
        self._model_status_lbl = tk.Label(head, text="检测中…", bg=CARD,
                                          fg=SUB, font=(FONT, 10))
        self._model_status_lbl.pack(side="right")
        self._model_bar = ttk.Progressbar(card, mode="determinate",
                                          maximum=100, value=0)
        self._model_bar.pack(fill="x", padx=18, pady=(6, 2))
        self._model_lbl = tk.Label(card, text="", bg=CARD, fg=TEXT,
                                   font=(FONT, 10), justify="left",
                                   wraplength=560, anchor="w")
        self._model_lbl.pack(anchor="w", padx=18, pady=(0, 4))
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=18, pady=(2, 14))
        self._model_retry_btn = ttk.Button(row, text="重试下载",
                                           command=self._model_retry,
                                           state="disabled")
        self._model_retry_btn.pack(side="left")
        self._model_skip_btn = ttk.Button(row, text="跳过（转写不可用）",
                                          command=self._model_skip)
        self._model_skip_btn.pack(side="left", padx=(10, 0))
        # ---- 人工下载通道（连续失败 >= 2 次后显示）----
        manual = tk.Frame(card, bg="#fdf0ec", highlightthickness=1,
                          highlightbackground="#f0c8b8")
        self._manual_frame = manual
        want = self._mdl.main_file_name(self.cfg)
        tk.Label(manual, text="自动下载多次失败 — 可人工下载模型文件后导入：",
                 bg="#fdf0ec", fg="#b3402a", font=(FONT, 11, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(manual, text=f"① 打开下方页面，下载文件「{want}」"
                              "（约数百 MB，其他小文件会自动补齐）：",
                 bg="#fdf0ec", fg=TEXT, font=(FONT, 10), justify="left",
                 wraplength=560, anchor="w").pack(fill="x", padx=12)
        btns = tk.Frame(manual, bg="#fdf0ec")
        btns.pack(fill="x", padx=12, pady=(4, 2))
        ttk.Button(btns, text="打开下载页面",
                   command=self._manual_open_page).pack(side="left")
        ttk.Button(btns, text="② 我已下载，导入…",
                   command=self._manual_import).pack(side="left", padx=(8, 0))
        self._manual_url_lbl = tk.Label(manual, text="", bg="#fdf0ec",
                                        fg=SUB, font=("Menlo", 9),
                                        justify="left", wraplength=560,
                                        anchor="w")
        self._manual_url_lbl.pack(fill="x", padx=12, pady=(2, 10))
        manual.pack_forget()  # 默认隐藏，失败次数达标后显示

    def _model_tick(self) -> None:
        """轮询模型下载状态并刷新第 1 步 UI（300ms 一次，后台线程更新）。"""
        try:
            if getattr(self, "_page", -1) != 0:
                return  # 已离开本页；返回时 _go(0) 会重建并重新轮询
            st = self._mdl.DownloadState.get().snapshot()
            state = st["state"]
            if state == "idle":
                # 首次进入：启动后台下载
                self._mdl.start_background(self.cfg)
                self._manual_hide()
                self._model_status_lbl.config(text="准备下载…", fg=SUB)
                self._model_lbl.config(
                    text="正在检查本地缓存并准备下载，请稍候…")
                self._next_btn.config(state="disabled")
            elif state == "running":
                phase, f = st["phase"], st["file_name"]
                pct = st["percent"]
                self._manual_hide()
                self._model_bar.config(value=pct)
                if f:
                    self._model_lbl.config(
                        text=f"正在下载 {phase} · {f}"
                             f"（{self._mdl.fmt_bytes(st['completed'])}"
                             f" / {self._mdl.fmt_bytes(st['total'])}"
                             f" · {pct:.0f}%）")
                else:
                    self._model_lbl.config(text=f"正在下载 {phase} …")
                self._model_status_lbl.config(text=f"下载中 {pct:.0f}%",
                                              fg=AMBER)
                self._next_btn.config(state="disabled")
            elif state == "error":
                self._model_bar.config(value=0)
                self._model_status_lbl.config(text="下载失败", fg=RED)
                self._model_lbl.config(
                    text=f"✗ 语音识别模型下载失败：{st['error'][:220]}\n"
                         "请检查网络（或关闭代理软件）后点「重试下载」；"
                         "若不下载，开始录音后将看不到转写内容。",
                    fg=RED)
                self._next_btn.config(state="disabled")
                self._model_retry_btn.config(state="normal")
                self._model_skip_btn.config(state="normal")
                # 连续失败 >= 2 次：亮出人工下载通道
                if st["fail_count"] >= 2:
                    self._manual_show()
                else:
                    self._manual_hide()
            else:  # done
                self._manual_hide()
                if st["missing"]:
                    # 声纹等可选部分缺失：不阻断录音，降级单人转写
                    self._model_bar.config(value=100)
                    self._model_status_lbl.config(
                        text="✓ 语音识别就绪", fg=GREEN)
                    self._model_lbl.config(
                        text="语音识别模型已就绪；部分辅助模型未下载完整，"
                             "将自动降级（不影响基本转写）。",
                        fg=SUB)
                    self._next_btn.config(state="normal")
                    self._model_retry_btn.config(state="normal")
                    self._model_skip_btn.config(state="disabled")
                    return
                self._model_bar.config(value=100)
                self._model_status_lbl.config(text="✓ 已就绪", fg=GREEN)
                self._model_lbl.config(
                    text="识别模型已就绪，开始录音即可实时显示转写。",
                    fg=GREEN)
                self._next_btn.config(state="normal")
                self._model_retry_btn.config(state="disabled")
                self._model_skip_btn.config(state="disabled")
                return  # 完成：停止轮询
            self.root.after(300, self._model_tick)
        except Exception:  # noqa: BLE001 轮询异常不影响向导其它步骤
            self.root.after(300, self._model_tick)

    def _welcome_next(self) -> None:
        """第 1 步「下一步」：模型就绪才放行（按钮在下载中已禁用）。"""
        st = self._mdl.DownloadState.get().snapshot()
        if st["state"] != "done":
            return
        self._go(1)

    def _model_retry(self) -> None:
        st = self._mdl.DownloadState.get()
        st._set(state="idle", error="", missing=self._mdl.missing(self.cfg))
        self._model_status_lbl.config(text="准备重试…", fg=SUB)
        self._model_retry_btn.config(state="disabled")
        self._model_skip_btn.config(state="normal")
        self.root.after(300, self._model_tick)

    def _model_skip(self) -> None:
        from tkinter import messagebox
        yes = messagebox.askyesno(
            "跳过模型下载",
            "语音识别模型未就绪时，点「开始录音」后不会显示实时转写内容。\n\n"
            "确定跳过？（可稍后在主界面点击「设置」→ 环境检查中重新下载）",
            parent=self.win or self.root)
        if yes:
            self._go(1)

    # ---------- 人工下载通道（自动下载连续失败 >= 2 次后亮出） ----------

    def _manual_show(self) -> None:
        try:
            urls = self._mdl.manual_urls(self.cfg)
            text = " ｜  ".join(f"{tag} {u}" for tag, u in urls)
            self._manual_url_lbl.config(text=text)
        except Exception:  # noqa: BLE001
            pass
        self._manual_frame.pack(fill="x", padx=18, pady=(0, 12))

    def _manual_hide(self) -> None:
        self._manual_frame.pack_forget()

    def _manual_open_page(self) -> None:
        import os
        import subprocess
        repo = self._mdl.whisper_repo(self.cfg)
        endpoint = os.environ.get("HF_ENDPOINT") or self._mdl.DEFAULT_HF_ENDPOINT
        url = f"{endpoint}/{repo}/tree/main"
        try:
            subprocess.Popen(["open", url])
        except Exception as e:  # noqa: BLE001
            self._model_lbl.config(text=f"无法打开浏览器：{e}", fg=RED)

    def _manual_import(self) -> None:
        from tkinter import filedialog
        want = self._mdl.main_file_name(self.cfg)
        path = filedialog.askopenfilename(
            parent=self.win or self.root,
            title=f"选择你已下载的模型文件（{want}）")
        if not path:
            return
        self._model_status_lbl.config(text="导入中…", fg=AMBER)
        self._model_lbl.config(
            text=f"正在导入 {want} 到本地模型目录，随后自动补齐其余文件，请稍候…",
            fg=AMBER)
        self._model_retry_btn.config(state="disabled")
        self._model_skip_btn.config(state="disabled")
        threading.Thread(target=self._manual_import_worker, args=(path,),
                         daemon=True).start()

    def _manual_import_worker(self, path: str) -> None:
        from . import model_download as mdl
        ok, msg = mdl.import_main_file(self.cfg, path)
        if not ok:
            self.root.after(0, lambda: self._model_show_manual_err(msg))
            return
        st = mdl.DownloadState.get()
        st._set(state="running", phase="补齐模型文件",
                file_name="", completed=0, total=0)
        try:
            mdl.ensure_missing(self.cfg, state=st)  # 小文件 + 声纹，完成即 done
        except Exception as e:  # noqa: BLE001
            st._set(state="error", error=str(e))
        # done/error 由 _model_tick 轮询刷新

    def _model_show_manual_err(self, msg: str) -> None:
        self._model_status_lbl.config(text="导入失败", fg=RED)
        self._model_lbl.config(text=f"✗ {msg}", fg=RED)
        self._model_retry_btn.config(state="normal")
        self._model_skip_btn.config(state="normal")

    def _checks(self) -> list[tuple[str, bool, str]]:
        out: list[tuple[str, bool, str]] = []
        out.append(("Python", True, f"v{sys.version.split()[0]}"))
        dep_ok, dep_info = self._check_deps()
        out.append(("语音识别依赖", dep_ok, dep_info))
        mic_ok, mic_info = self._check_mic()
        out.append(("麦克风", mic_ok, mic_info))
        return out

    @staticmethod
    def _check_deps() -> tuple[bool, str]:
        try:
            import faster_whisper
            return True, f"faster-whisper {faster_whisper.__version__} 已就绪"
        except Exception:  # noqa: BLE001
            return False, "未安装，请重新执行 install.sh 安装依赖"

    @staticmethod
    def _check_mic() -> tuple[bool, str]:
        try:
            import sounddevice as sd
            devs = [str(d["name"]) for d in sd.query_devices()
                    if d.get("max_input_channels", 0) > 0]
            if devs:
                return True, f"检测到 {len(devs)} 个输入设备: {'、'.join(devs[:2])}"
            return False, "未找到输入设备，请连接麦克风"
        except Exception:  # noqa: BLE001
            return False, "音频库初始化失败（见日志）"

    # ---------- LLM ----------

    def _page_llm(self) -> None:
        card = self._card()
        tk.Label(card, text="AI 纪要（可选，建议填写）", bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w", padx=18, pady=(16, 2))
        conf = summarize.is_configured()
        self._note(card, "填写后，点「结束会议」时自动生成结构化会议纪要。"
                         "留空可跳过——录音与实时转写不受任何影响，"
                         "稍后可在主界面「设置」补填。",
                   color="#2956a3" if not conf else GREEN)

        self._v = {
            "url": tk.StringVar(value=self.env.get("LLM_BASE_URL")
                                or ENV_DEFAULTS["LLM_BASE_URL"]),
            "key": tk.StringVar(value=self.env.get("LLM_API_KEY", "")),
            "model": tk.StringVar(value=self.env.get("LLM_MODEL")
                                  or ENV_DEFAULTS["LLM_MODEL"]),
            "ssl": tk.BooleanVar(value=self.env.get("LLM_SSL_VERIFY", "true")
                                 .lower() in ("false", "0", "no")),
        }
        rows = [
            ("接口地址", self._v["url"], None),
            ("API Key", self._v["key"], "•" * 20),
            ("模型名", self._v["model"], None),
        ]
        for i, (label, var, show) in enumerate(rows):
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", padx=18, pady=4)
            tk.Label(row, text=label, bg=CARD, fg=TEXT, width=10,
                     font=(FONT, 11), anchor="w").pack(side="left")
            tk.Entry(row, textvariable=var, show=show, relief="solid",
                     bd=1, highlightthickness=0, font=(FONT, 11)
                     ).pack(side="left", fill="x", expand=True)
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=18, pady=(8, 2))
        ttk.Checkbutton(row, text="跳过 SSL 证书校验（公司内网/自签证书场景）",
                        variable=self._v["ssl"]).pack(side="left")
        # 连接测试（后台线程，不卡界面）
        test_row = tk.Frame(card, bg=CARD)
        test_row.pack(fill="x", padx=18, pady=(0, 16))
        ttk.Button(test_row, text="测试连接", command=self._test_llm
                   ).pack(side="left")
        self._test_lbl = tk.Label(test_row, text="", bg=CARD, fg=SUB,
                                  font=(FONT, 10), justify="left",
                                  wraplength=470, anchor="w")
        self._test_lbl.pack(side="left", padx=(12, 0), fill="x", expand=True)

        def skip() -> None:
            self._v["key"].set("")
            self._go(self._page + 1)

        self._footer(extra=lambda f: ttk.Button(f, text="跳过此步",
                                                command=skip).pack(side="left"))

    # ---------- 连接测试（LLM 页）----------

    def _test_llm(self) -> None:
        v = self._v
        key = v["key"].get().strip()
        if not key or summarize._placeholder(key):
            self._show_test(False, "请先填写 API Key（占位内容不可用）")
            return
        url = v["url"].get().strip()
        model = v["model"].get().strip() or ENV_DEFAULTS["LLM_MODEL"]
        ssl_skip = v["ssl"].get()
        self._show_test(None, "正在测试连接…")
        threading.Thread(target=self._test_worker,
                         args=(url, key, model, ssl_skip), daemon=True).start()

    def _test_worker(self, url: str, key: str, model: str,
                     ssl_skip: bool) -> None:
        ok, msg = summarize.ping(url, key, model, verify_ssl=not ssl_skip)
        self.root.after(0, lambda: self._show_test(ok, msg))

    def _show_test(self, ok: bool | None, msg: str) -> None:
        lbl = getattr(self, "_test_lbl", None)
        if lbl is None:
            return
        color = SUB if ok is None else (GREEN if ok else RED)
        lbl.config(text=msg, fg=color)

    # ---------- 保存目录 ----------

    def _page_dir(self) -> None:
        card = self._card()
        tk.Label(card, text="纪要保存位置", bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w", padx=18, pady=(16, 2))
        self._note(card, "会议结束时自动生成 Markdown 纪要（及转写全文 .md）"
                         "并保存到该文件夹。默认：文档/会议纪要。")
        # 修复 2026-10-20：此前整体覆盖 self._v 导致第 2 步填的
        # API Key 丢失、完成时不写 .env（"已配置仍提示未配置"）。
        # 改为只追加「dir」键，保留 LLM 四键。
        self._v["dir"] = tk.StringVar(value=self.cfg["output"].get("obsidian_dir", "")
                                      or cfg_mod.OBSIDIAN_DIR_DEFAULT)
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=18, pady=(4, 16))
        tk.Entry(row, textvariable=self._v["dir"], relief="solid", bd=1,
                 highlightthickness=0, font=(FONT, 11)
                 ).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", width=8,
                   command=self._pick_dir).pack(side="left", padx=(8, 0))
        # 修复 2026-10-20：此前漏调 _footer，第 3 步没有「下一步」按钮
        self._footer(next_text="下一步  >")

    def _pick_dir(self) -> None:
        init = self._v.get("dir")
        cur = init.get() if init else ""
        d = filedialog.askdirectory(parent=self.win or self.root,
                                    initialdir=cur or str(cfg_mod.OBSIDIAN_DIR_DEFAULT),
                                    title="选择纪要保存文件夹")
        if d:
            self._v["dir"].set(d)

    # ---------- 完成 ----------

    def _page_done(self) -> None:
        card = self._card()
        tk.Label(card, text="全部就绪", bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        key = (self._v.get("key") or tk.StringVar()).get().strip()
        llm_conf = bool(key) and not summarize._placeholder(key)
        d = self._v["dir"].get().strip() or cfg_mod.OBSIDIAN_DIR_DEFAULT
        rows = [
            ("AI 纪要",
             ("已配置（模型 " + ((self._v.get("model") or tk.StringVar())
                                 .get().strip() or ENV_DEFAULTS["LLM_MODEL"]) + "）")
             if llm_conf else "未配置（可稍后点「设置」补填）",
             llm_conf),
            ("纪要保存目录", d, True),
        ]
        for label, val, ok in rows:
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", padx=18, pady=3)
            tk.Label(row, text=label, bg=CARD, fg=SUB, width=12,
                     font=(FONT, 11), anchor="w").pack(side="left")
            tk.Label(row, text=val, bg=CARD, fg=TEXT if ok else AMBER,
                     font=(FONT, 11), justify="left", wraplength=420
                     ).pack(side="left", anchor="w")
        self._note(card, "点击「开始使用」进入主界面开始录音。"
                         "之后可在主界面随时点「设置」修改以上配置。",
                   color="#2956a3")
        self._footer(next_text="✓  开始使用", on_next=self._finish)

    # ================= 落盘 =================

    def _finish(self) -> None:
        v = self._v
        key = (v.get("key") or tk.StringVar()).get().strip()
        if key and not summarize._placeholder(key):
            env = dict(self.env)
            env["LLM_BASE_URL"] = (v.get("url").get().strip()
                                   or ENV_DEFAULTS["LLM_BASE_URL"])
            env["LLM_API_KEY"] = key
            env["LLM_MODEL"] = (v.get("model").get().strip()
                                or ENV_DEFAULTS["LLM_MODEL"])
            env["LLM_SSL_VERIFY"] = "false" if v["ssl"].get() else "true"
            self._write_env(env)
        d = (v.get("dir").get().strip() or cfg_mod.OBSIDIAN_DIR_DEFAULT)
        self.cfg["output"]["obsidian_dir"] = d
        self.cfg["wizard_done"] = True
        cfg_mod.save(self.cfg)
        self.done = True
        self._on_close()

    @staticmethod
    def _write_env(env: dict) -> None:
        lines = [
            "# 会议录音助手 — LLM 配置（由配置向导/GUI 设置生成）",
            "# 说明：未配置时仍可录音+实时转写；仅「结束会议 → AI 生成纪要」需要。",
            "",
            "# ===== AI 纪要接口（OpenAI 兼容协议）=====",
            f"LLM_BASE_URL={env.get('LLM_BASE_URL', '')}",
            f"LLM_API_KEY={env.get('LLM_API_KEY', '')}",
            "",
            f"LLM_MODEL={env.get('LLM_MODEL', '')}",
            "",
            "# 公司内网/自签证书场景设为 false 跳过证书校验",
            f"LLM_SSL_VERIFY={env.get('LLM_SSL_VERIFY', 'true')}",
            "",
        ]
        (cfg_mod.APP_DIR / ".env").write_text("\n".join(lines), encoding="utf-8")
