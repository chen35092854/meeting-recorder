# WorkBuddy 会议录音 · 实时转写与 AI 纪要

本地会议录音工具：调用 Mac 麦克风，**按音色自动区分发言人（声纹分人）**，会议中**实时显示分声轨转写**；会议结束点击按钮后，自动调用 `.env` 中配置的文本模型接口（默认 `deepseek-v4-flash-2`），按 `prompt/` 目录的约束与模板生成**六模块会议纪要**，保存到 Obsidian 指定文件夹并同步进度。

## 功能特性

| 能力 | 说明 |
|---|---|
| 实时多说话人转写 | faster-whisper（本地识别，隐私安全），SpeechBrain ECAPA 声纹聚类区分发言人 |
| 多语种自适应 | 中文输出简体中文；其他语种（英/日/韩/法等）按现场实际语种输出并标注 |
| GUI 交互 | 主题可填、设备可选（含系统默认）、实时彩色分人转写、输入电平条（dB）、结束会议按钮、AI 纪要进度条 |
| AI 纪要（六模块） | 结束会议后自动调用 `.env` 接口（deepseek-v4-flash-2）生成：基本信息/核心结论/议题讨论/决策/行动计划/风险；约束与模板见 `prompt/` 目录，可直接编辑 |
| 存档完整 | 原始录音 WAV + 转写全文 MD + 结构化 JSON 均本地保留 |
| Obsidian 集成 | 纪要 + 转写全文自动写入坚果云 Obsidian 库「03 会议纪要」目录 |

## 快速开始

### 1. 安装依赖（首次一次性）
```bash
cd /Users/eric/WorkBuddy/2026-10-16-16-03-31/meeting-recorder
bash install.sh
```
说明：创建独立 `.venv` 虚拟环境；音频采集优先使用自带音频库的 PyAudio/sounddevice/PyAV，无需 Homebrew；语音模型首次运行自动下载（走国内镜像 hf-mirror.com）。

### 2. 启动录音（三种方式任选）
- **快捷指令**：在 WorkBuddy 对话输入 `/meeting-recorder`，或同义指令 `/录音`、`/会议录制`、`/开始会议`（自然语言"开始录音/开会了"同样有效）；
- **双击（推荐）**：`MeetingRecorder.app`（标准应用，无需终端窗口，直接弹出 GUI）；
- **双击（备选）**：`launch.command`（会打开终端窗口，录音期间请勿关闭）；
- **命令行**：`arch -arm64 ./.venv/bin/python run_recorder.py`。

**双击启动说明**：
1. Finder 中按 `⌘⇧G` 输入 `/Users/eric/WorkBuddy/2026-10-16-16-03-31/meeting-recorder` 回车定位；
2. 双击 `MeetingRecorder.app` → GUI 直接弹出（推荐，无终端窗口）；双击 `launch.command` → 会打开"终端"窗口启动，**录音期间请勿关闭该终端窗口**；
3. 首次双击若被 macOS 拦截：右键 → 「打开」→ 再点「打开」；
4. 日常便捷：右键 `MeetingRecorder.app` → 「制作替身」或将 App 直接拖到程序坞，以后一键启动。

会议结束后**自动生成纪要并保存 Obsidian，全程无需回到对话**（仅当 `.env` 未配置 Key 或接口失败时，回对话发送“生成会议纪要”可兜底生成）。

首次启动若无法录音：系统设置 → 隐私与安全性 → 麦克风 → 允许（终端/WorkBuddy）。

### 3. 配置文本模型接口（.env，一次性）
打开 `meeting-recorder/.env`：
```
LLM_BASE_URL=https://ai.ciic.com/llm/apis/ais-v2/chat/completions   # 接口地址（OpenAI 兼容）
LLM_API_KEY=你的Key                                                  # 修改此处填入 Key
LLM_MODEL=deepseek-v4-flash-2                                        # 模型名
LLM_SSL_VERIFY=true                                                  # 内网自签证书报 SSL 错误时改 false
```
保存后生效（下次点击「结束会议」时读取）。接口为 OpenAI 兼容格式（`/chat/completions`）。

### 4. 使用流程
1. 点击「开始录音」→ 观察**输入电平条**（说话时跳动）确认正在录音；实时查看分声轨转写（不同发言人不同颜色）；
2. 会议结束 → 点击「结束会议（生成纪要）」→ 转写自动保存，随后自动调用接口生成纪要（进度条推进）；
3. 进度条走满 100% → 纪要已写入 Obsidian「03 会议纪要」，可点击「打开纪要文件夹」核验。

## 文件说明

```
meeting-recorder/
├── run_recorder.py          # 入口（GUI）
├── install.sh               # 一键安装依赖
├── launch.command           # 双击启动
├── config.json              # 配置（模型、路径、声纹阈值等）
├── .env                     # 文本模型接口：地址 / Key（自行填写）/ 模型名
├── requirements.txt
├── app/
│   ├── recorder.py          # 麦克风采集（多后端自适应）
│   ├── asr.py               # 语音识别（faster-whisper）
│   ├── diarize.py           # 声纹分人（ECAPA 嵌入 + 余弦聚类）
│   ├── pipeline.py          # 处理管线与状态机
│   ├── asr_mlx.py           # 语音识别（MLX Whisper，Apple Silicon 加速）
│   ├── punctuate.py         # 中文转写断句恢复（空格→逗号、引导词补逗号、句末句号）
│   ├── summarize.py         # 纪要自动生成（调用 .env 接口，OpenAI 兼容）
│   ├── store.py             # 存档（WAV/转写MD/JSON/状态文件）
│   └── gui.py               # tkinter 界面
├── prompt/                  # AI 纪要生成约束（可直接编辑，无需改代码）
│   ├── system_prompt.md     # 生成规则（内容原则/结论提炼/议题/决策/行动项/真实性/输出）
│   ├── template.md          # 纪要 Markdown 骨架（六个一级模块 + 转写全文链接）
│   ├── length_guide.md      # 按会议类型的推荐纪要长度
│   └── README.md            # 使用说明
└── recordings/              # 录音与转写存档
    ├── {会议ID}.wav
    ├── {会议ID}_transcript.json   # 结构化转写（AI 纪要输入）
    ├── {会议ID}_transcript.md     # 转写全文
    └── current_status.json        # GUI 与 AI 协作状态
```

## 配置（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `asr.model_size` | `small` | tiny/base/small/medium/large-v3；small 为中文效果/速度均衡 |
| `asr.language` | `auto` | `auto`=按语种输出；固定为 `zh` 则强制中文 |
| `asr.punctuate` | `true` | 转写断句恢复：Whisper 空格→逗号、从句引导词前补逗号、句末补句号（关闭置 `false`） |
| `asr.simplify_zh` | `true` | 中文统一转简体 |
| `asr.chunk_seconds` | `8` | 实时处理分块时长（越小越实时，越大越稳） |
| `audio.silence_rms` | `0.002` | 分块能量低于该值且峰值也低于 `voice_peak` 才判为静音跳过 |
| `diarize.threshold` | `0.72` | 声纹相似度阈值；误分人调高，漏分人调低 |
| `output.obsidian_dir` | 03 会议纪要目录 | 纪要保存位置 |
| `hf_endpoint` | `https://hf-mirror.com` | 模型下载镜像（大陆网络必需） |

### .env（文本模型接口，GUI 自动生成纪要用）

| 键 | 说明 |
|---|---|
| `LLM_BASE_URL` | OpenAI 兼容接口地址（含 `/chat/completions` 完整路径） |
| `LLM_API_KEY` | API Key（自行填写） |
| `LLM_MODEL` | 模型名，默认 `deepseek-v4-flash-2` |
| `LLM_SSL_VERIFY` | 默认 `true`；内网/网关自签证书报 SSL 错误时改为 `false` |

## 常见问题

- **启动闪退（numpy 架构错误）**：本机依赖均为 Apple Silicon 原生（arm64）版本；若经终端/LaunchServices 启动时被切换到 Rosetta（x86_64）会报 `incompatible architecture`。所有官方启动器（`.app`、`launch.command`）已内置 `arch -arm64` 强制原生架构；若仍出现，手动执行 `arch -arm64 ./.venv/bin/python run_recorder.py`。
- **模型下载失败**：检查网络；或改为官方源 `export HF_ENDPOINT=https://huggingface.co` 后重试。
- **无法录音**：麦克风授权（系统设置→隐私与安全性）；或 GUI 中更换输入设备（外接麦克风）。
- **分人不准**：阈值调高到 0.75~0.80 更保守（少误分），调低更敏感（少合并）。
- **转写延迟**：`chunk_seconds` 调小（默认 8）更实时；CPU 性能不足可换 `base` 模型。
- **一直无转写**：先看**输入电平条**是否跳动——不动则未采到音（麦克风权限/输入设备），会显示红色警告；电平正常但仍无字，查看 `logs/recorder.log` 中分块 rms/peak 日志。
- **纪要未自动生成**：确认 `.env` 已填 `LLM_API_KEY`（接口地址/模型名已就绪）；内网证书报错 → 设 `LLM_SSL_VERIFY=false`；仍不行可回对话发送"生成会议纪要"由对话内置模型兜底生成。

## 隐私说明

录音与转写全部在本机完成（faster-whisper / SpeechBrain 本地推理）；「生成纪要」一步将**转写文本**发送到 `.env` 配置的接口（deepseek-v4-flash-2）生成纪要，原始录音不上传。
