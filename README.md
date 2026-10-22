# 本地语音链路（听 + 说）

选型：SenseVoice-Small 负责「听」，CosyVoice-300M-SFT 负责「说」，「想」走云端 agent，不留本地大脑。
两个模型许可均为 Apache 2.0（SenseVoice 见魔搭模型页；CosyVoice 见 `CosyVoice/LICENSE` 正本）。
本地不保留人格大脑；云端 profile 切换已实现（`bridge/brains.py`、`/brain`），用量账本写 `~/.voice-ai/usage.jsonl`；五层记忆阶段 1–5 已完成（默认 `MEMORY_ENABLED=0`、`MEMORY_DRY_RUN=1`，真实目录启用待确认）；规则命令（stop/time/timer）已在 `pipeline.Turn` 本地短路，不调云端；本地小模型仍是设计，见 `bridge/README.md`。
当前进度、文件清单和下一步入口见 `HANDOFF.md`；测试命令见 `HANDOFF.md` §9；硬件上机按 `handoff/G-hardware-bringup.md`；B/C/F/G 四份外包提示词在 `handoff/`（B/C/F 已归档，G 现役）。

## 目录

| 路径 | 内容 |
|---|---|
| `.venv/` | Python 3.13.16 环境，跑 ASR（torch 2.14.1+cu126、funasr 1.4.16） |
| `py311/` | 嵌入式 Python 3.11.9 + pip，跑 TTS（官方 installer 在本机非管理员环境下会卡死，见下） |
| `bridge/` | PC 桥：SenseVoice → 云端 LLM → TTS，规则命令、五层记忆模块与测试；契约见 `bridge/README.md` |
| `models/SenseVoiceSmall/` | 936 MB，来自魔搭 `iic/SenseVoiceSmall` |
| `models/CosyVoice-300M-SFT/` | 1.9 GB，只取推理必需的 8 个文件（完整仓库 5.36 GB，其余是 zip/onnx 导出版） |
| `CosyVoice/` | 官方源码 + `third_party/Matcha-TTS`，经 codeload tarball 获取（`github.com` 的 git 协议在本机被连接重置） |
| `test-data/` | `zh.mp3` 测试音频、`tts_out.wav` 合成结果 |
| `firmware/` | ESP32-S3 固件：录音/HTTP/chunked 播放（三模式已编译通过，未上机） |
| `asr.py` / `asr_test.py` | 语音识别入口 |
| `tts_test.py` | 语音合成入口，输出 `tts_report.json` |

完全卸载 = 删除本目录，不动系统 PATH 与注册表。

## 已实测

**听（SenseVoice-Small，GPU）**：识别 `test-data/zh.mp3` → 「开饭时间早上9点至下午五点。」，附 `<|zh|><|NEUTRAL|>` 语言/情绪标签。**一句话 0.06 秒**（RTF 0.015），模型加载 2.4 秒。

**说（CosyVoice-300M，GPU，单进程无争用）**：

| 项 | 数字 |
|---|---|
| RTF | **0.96–1.22，全模式贴着 1.0** —— 这张 3060 对 300M 的天花板 |
| 流式首包 | 2.46 秒（fp32，28 字切 3 块）／Instruct 版 2.19–3.63 秒 |
| 冷加载 | 53.9 秒（SFT）/ 64.3 秒（Instruct） |
| 同进程热加载 | **7.5–9.8 秒** → 必须常驻，不能每句重载 |
| 音色 | 7 个：中文女/中文男/日语男/粤语女/英文女/英文男/韩语女（SFT 与 Instruct 完全相同） |

**两个反直觉结论：**

- **fp16 是负优化。** 整句 7.18 秒 vs fp32 的 5.73 秒，首包 2.81 vs 2.46，连加载都更慢。`tts_bench.py` 默认 fp32，`--fp16` 才开启。
- **`inference_instruct` 的指令必须以 `<|endofprompt|>` 结尾。** 少了它，模型会把指令原文当正文念出来（实测时长从 3.5 秒涨到 5.6–6.0 秒，ASR 转写可见整条指令）。补上后转写干净。

**情感控制的实际效果**（`instruct2_test.py`，同句「今天天气真不错，我们一起出去玩吧。」）：作用在**语速和音量**上——开心 2.75 秒、愤怒 2.91 秒 vs 无指令 3.65 秒（快 20–25%），愤怒 RMS 0.079 vs 悲伤/开心 0.124；但**音高完全分不开**——F0 中位数 209.6/214.2/215.0/218.7 Hz，组间差最大 9.1 Hz，而组内标准差 60–75 Hz。**每种情绪只合成过一次**，而种子本身就能造成 20% 起伏，所以「情感到底可控」尚未定论，需要 3–5 个种子重复。SenseVoice 对四条输出全部标记为 `HAPPY`。

验证方法论（值得复用）：把 TTS 输出**回灌自己的 ASR** 看实际念了什么，再用 librosa YIN 量基频/时长/能量。「不报错」不等于「功能生效」。注意本机 `librosa.yin` 返回裸 ndarray，不是文档写的三元组。

## 本机踩过的两个坑

1. 嵌入式 Python 3.11 里装 CosyVoice 依赖时，`openai-whisper==20231117` 因 setuptools ≥81 移除了 `pkg_resources` 而构建失败，需先 `pip install "setuptools<81"`。pip 失败时若命令结尾有 `echo`，后台任务会误报退出码 0，退出码要单独落到日志里。
2. Git Bash/MSYS 会改写 Windows 安装器参数（`/quiet` 变成路径、`TargetDir=C:\...` 的反斜杠被吃掉），导致 MSI 报错误 53 并挂住。此类命令应走 `powershell -NoProfile -Command`。

## 显存约束

SenseVoice 约 1 GB、CosyVoice 约 2 GB，可同时挂 GPU。若日后重新安装本地大模型，这条约束需要重新评估。
