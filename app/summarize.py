# -*- coding: utf-8 -*-
"""AI 会议纪要生成：调用 .env 中配置的 OpenAI 兼容接口自动生成纪要。

流程：结束会议（转写已保存 transcript_ready）→ 本模块读取 .env 的
LLM_BASE_URL / LLM_API_KEY / LLM_MODEL → 读取 prompt/ 目录的生成约束与模板 →
调用接口生成「六模块」纪要 → 保存到 Obsidian（纪要 + 转写全文两份文件，
纪要文末自动附转写全文链接）→ 更新 status=done。

prompt/ 目录（system_prompt.md / template.md / length_guide.md）为生成约束的
唯一来源，用户可直接编辑文件调整生成行为，无需改代码；缺文件时回退内置默认。
未配置 .env 时返回错误，可退回「WorkBuddy 对话内置模型」通道（skill 场景二）。
零外部依赖（仅标准库 urllib）。
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.request
from pathlib import Path

from . import config as cfg_mod
from . import store

logger = logging.getLogger(__name__)

ENV_PATH = cfg_mod.APP_DIR / ".env"

PROMPT_DIR = cfg_mod.APP_DIR / "prompt"

# 兜底默认（prompt/ 目录文件缺失时使用；日常生成以 prompt/ 目录内容为准，
# 用户可直接编辑 prompt/ 下的文件自定义生成约束）
DEFAULT_SYSTEM_PROMPT = """你是专业会议纪要助手。请依据提供的会议转写，生成「六模块」会议纪要 Markdown。

硬性规范：
1. 正文使用简体中文书面语；发言人其他语种的现场发言，在「主要议题与讨论要点」引用时保留原语，可附中文翻译。
2. 不得虚构：议题、决策、行动项、负责人必须能在转写中找到依据；找不到的用"—"或省略。
3. 发言人命名沿用转写中的「发言人1」「发言人2」；转写中可推断真实身份时（如“王总说”），标注为「发言人1（王总）」。
4. 会议时间用转写中的实际开始/结束时间，时长取整分钟。
5. 大标题必须为 `# 会议纪要｜{主题}`，主题由转写内容概括（不超过 20 字）。
6. 只输出纪要正文 Markdown（不要解释、不要包裹代码块、不要输出 front matter）。
7. 不要在文末输出「转写全文」链接或 AI 免责声明——保存程序会统一追加。

模板骨架（严格按此结构，无实质内容的模块可省略）：
# 会议纪要｜{会议主题}
## 一、会议基本信息
- 会议时间：…
- 会议形式：…
- 参会人员：…
- 会议主题：…
## 二、会议核心结论
> 用3–5条概括本次会议最重要的结论与成果。
1. **{核心结论一}**：…
## 三、主要议题与讨论要点
### 议题一：{议题名称}
- **讨论背景**：…
- **关键观点**：…
- **讨论结果**：…
（按实际讨论内容增减议题，按议题归类而非按发言人归类）
## 四、会议决策事项
| 序号 | 决策事项 | 决策内容 | 关键条件 |
| 1 | … | … | … |
（仅记录明确确认的决定，无则省略/标注暂无）
## 五、后续行动计划
| 序号 | 行动任务 | 负责人 | 截止时间 | 预期交付物 |
| 1 | … | … | … | … |
（未明确的信息标注待明确，不自行推断）
## 六、待解决问题与风险
- **待确认事项**：…
- **主要风险**：…
- **后续跟进**：…
"""


def load_system_prompt() -> str:
    """从项目 prompt/ 目录加载生成约束（system_prompt.md + template.md + length_guide.md），
    缺文件时回退到内置默认。用户可直接编辑 prompt/ 下文件自定义生成行为，无需改代码。"""
    def _read(name: str) -> str:
        p = PROMPT_DIR / name
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    rules = _read("system_prompt.md")
    skeleton = _read("template.md")
    length = _read("length_guide.md")
    parts: list[str] = []
    if rules:
        parts.append(rules)
    if skeleton:
        parts.append("# 输出格式（严格按此结构，无实质内容的模块可省略）\n\n" + skeleton)
    if length:
        parts.append("# 篇幅建议（按会议信息密度动态调整，非强制字数上限）\n\n" + length)
    if not parts:
        return DEFAULT_SYSTEM_PROMPT
    head = ("你是专业会议纪要助手。请依据提供的会议转写，生成会议纪要 Markdown。"
            "以下规则与模板来自项目 prompt/ 目录，必须严格执行；不得虚构内容，只能依据转写。")
    tail = ("\n\n硬性规范：\n"
            "1. 正文使用简体中文书面语；其他语种现场发言在引述时保留原语，可附中文翻译。\n"
            "2. 发言人命名沿用转写中的「发言人1」「发言人2」；可推断真实身份时标注如「发言人1（王总）」。\n"
            "3. 会议时间用转写中的实际开始/结束时间，时长取整分钟。\n"
            "4. 大标题按模板 `# 会议纪要｜{主题}`，主题由转写内容概括（不超过 20 字）；模板中的 {占位} 替换为实际值。\n"
            "5. 只输出纪要正文 Markdown（不要解释、不要包裹代码块、不要输出 front matter）。\n"
            "6. 不要在文末输出「转写全文」链接或 AI 免责声明——保存程序会统一追加。")
    return head + "\n\n" + "\n\n".join(parts) + tail


def load_env() -> dict:
    """读取项目根目录 .env（Key 由用户自行填写）。"""
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _placeholder(v: str) -> bool:
    """API Key 是否仍为占位（用户尚未填写）。"""
    v = (v or "").strip().lower()
    return (not v) or any(t in v for t in ("请填写", "your key", "xxxx", "sk-xxx", "替换"))


def is_configured() -> bool:
    env = load_env()
    return bool(env.get("LLM_BASE_URL")) and (not _placeholder(env.get("LLM_API_KEY")))


def _chat(base_url: str, api_key: str, model: str, messages: list[dict],
          timeout: int = 600, verify_ssl: bool = True) -> tuple[str, str]:
    """调用 OpenAI 兼容接口，返回 (content, finish_reason)。"""
    base = base_url.rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4000,
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    ctx = None
    if not verify_ssl:  # 公司网关/内网证书场景：.env 设 LLM_SSL_VERIFY=false
        ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        choice = data["choices"][0]
        return (choice["message"]["content"] or "",
                str(choice.get("finish_reason") or "stop"))
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"接口返回格式异常: {str(data)[:300]}") from e


def _is_minutes_complete(text: str, finish: str) -> bool:
    """纪要输出完整性判定：finish_reason 非 length，且至少 3 个二级模块
    （模板固定六模块，允许无内容模块省略——正常输出至少包含
    基本信息/核心结论/其他模块，实测截断输出只有 1~2 个二级标题）。"""
    if not text or finish == "length":
        return False
    return len(re.findall(r"^## ", text, re.M)) >= 3


def _fmt_transcript(meta: dict, utterances: list[dict]) -> str:
    """把逐句转写整理成供模型阅读的文本。"""
    lines = [
        f"会议时间：{meta['started_at']:%Y-%m-%d %H:%M} ~ {meta['ended_at']:%H:%M}，"
        f"时长 {int(meta['duration'] // 60)} 分钟",
        "逐句转写（含发言人/时间/语种）：",
    ]
    for u in utterances:
        ts = store.ts_fmt(u["start"])
        lang = f"[{store.lang_label(u.get('lang'))}]" \
            if u.get("lang") not in (None, "", "zh") else ""
        lines.append(f"- {u['speaker']}（{ts}）{lang}：{u['text']}")
    return "\n".join(lines)


def _safe(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")
    return s or "会议"


def _pick_theme(text: str, fallback: str) -> str:
    """从模型输出的首个 Markdown 标题提取主题（≤20 字），失败时回退。"""
    m = re.search(r"^#\s+(.+)$", text.strip(), re.M)
    theme = m.group(1).strip() if m else ""
    theme = re.sub(r"^会议纪要\s*[｜|:：]?\s*", "", theme)
    if len(theme) > 20:
        theme = theme[:20].rstrip("。，；")
    if not theme:
        theme = fallback if fallback and fallback != "会议" else ""
    return theme


def generate_minutes(cfg: dict, meta: dict, utterances: list[dict]) -> tuple[bool, str, dict]:
    """生成纪要并保存到 Obsidian；更新 current_status.json。"""
    env = load_env()
    base_url = env.get("LLM_BASE_URL", "").strip()
    api_key = env.get("LLM_API_KEY", "").strip()
    if not base_url or _placeholder(api_key):
        return False, "llm_not_configured", {}
    model = env.get("LLM_MODEL", "").strip() or "deepseek-v4-flash-2"
    verify_ssl = env.get("LLM_SSL_VERIFY", "true").strip().lower() not in ("false", "0", "no")

    def _status(**kw) -> None:
        st = store.read_status() or {}
        st.update({"state": "generating", "progress": 35, "stage": "AI 正在生成纪要…",
                   "error": None, "minutes_obsidian_path": None,
                   "transcript_obsidian_path": None}, **kw)
        store.write_status(st)

    _status(progress=35, stage="AI 正在生成纪要…")
    messages = [
        {"role": "system", "content": load_system_prompt()},
        {"role": "user", "content": _fmt_transcript(meta, utterances)},
    ]
    # 偶发截断防重（2026-10-19）：deepseek 接口偶发在开头即停止输出
    # （实测输出约 140 字符、只有 1~2 个二级标题），加上 finish_reason 与
    # 模块完整性校验，不完整时自动重试一次，避免落盘残缺纪要。
    text, finish = "", "stop"
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            text, finish = _chat(base_url, api_key, model, messages,
                                 verify_ssl=verify_ssl)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt == 1:
                continue  # 接口异常重试一次
            break
        if _is_minutes_complete(text, finish) or attempt == 2:
            break
        logger.warning("纪要输出不完整（finish=%s，二级标题=%d），正在重试…",
                       finish, len(re.findall(r"^## ", text, re.M)))
    if last_exc is not None and not text:
        hint = ""
        if isinstance(last_exc, urllib.error.URLError) and "CERTIFICATE_VERIFY_FAILED" in str(last_exc):
            hint = "（如为公司内网/网关环境，可在 .env 中把 LLM_SSL_VERIFY 设为 false 跳过证书校验）"
        _status(state="error", progress=0, stage="模型调用失败",
                error=f"模型接口调用失败：{last_exc}{hint}")
        return False, f"模型接口调用失败：{last_exc}{hint}", {}

    _status(progress=75, stage="保存纪要到 Obsidian…")
    try:
        obs_dir = Path(cfg["output"]["obsidian_dir"])
        obs_dir.mkdir(parents=True, exist_ok=True)
        date_str = meta["started_at"].strftime("%Y-%m-%d")
        theme = _pick_theme(text, meta.get("title", ""))
        suffix = f"-{_safe(theme)}" if theme else ""

        minutes_path = obs_dir / f"会议纪要-{date_str}{suffix}.md"
        transcript_name = f"转写全文-{date_str}{suffix}.md"

        body = text.strip()
        # 文末统一追加「转写全文」相对链接 + AI 免责声明（模型输出已含则不重复）
        link = f"📎 **转写全文**：[{transcript_name}]({transcript_name})"
        if "**转写全文**" not in body:
            body += f"\n\n---\n{link}"
        if "本纪要由AI" not in body:
            body += "\n\n*本纪要由AI根据会议录音整理。重要决策、负责人及时间节点建议人工确认。*"
        minutes_path.write_text(body + "\n", encoding="utf-8")

        transcript_path = obs_dir / transcript_name
        transcript_path.write_text(store.build_transcript_md(meta, utterances),
                                   encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _status(state="error", progress=0, stage="保存失败", error=f"保存纪要失败：{e}")
        return False, f"保存纪要失败：{e}", {}

    _status(state="done", progress=100, stage="完成",
            minutes_obsidian_path=str(minutes_path),
            transcript_obsidian_path=str(transcript_path))
    return True, str(minutes_path), {"minutes": str(minutes_path),
                                     "transcript": str(transcript_path)}
