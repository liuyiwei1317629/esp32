# 小鱼语音链路 · 运行环境与启动说明（RUNBOOK）

> 目的：换一台电脑、或者过几个月回来，只看这一页就能把项目跑起来。
> 最后核对：2026-10-28（本地大脑 qwen3.5:2b + 调试台记忆编辑/一键关闭 均实测通过）

---

## 1. 这套东西由什么组成

```
ESP32-S3 设备                 Windows PC（本机）                      外网
─────────────                ──────────────────────────────          ──────
麦克风 ICS43434  ──录音──▶    bridge（Python，:8080）
                              ├─ ASR   SenseVoice-Small（本机 GPU）
                              ├─ LLM   brains.toml 里选的 profile ──▶ DeepSeek / Ollama(本机 :11434)
                              └─ TTS   肥鱼(WSL) → CosyVoice(Windows) → edge(在线兜底)
功放 MAX98357A  ◀──PCM──     返回 16k 单声道 PCM 流
```

- 设备只负责「录 + 放」，所有理解、生成、合成都在 PC 上。
- 调试台（浏览器）：`http://127.0.0.1:8080/console`，能发文字/录音、看每轮耗时、切大脑、看改记忆、一键关闭。
- 契约细节（接口、字段、错误码）：`bridge/README.md`。硬件上机：`handoff/G-hardware-bringup.md`。进度与下一步：`HANDOFF.md`。

## 2. 环境要求

| 项 | 要求 | 本机实况 |
|---|---|---|
| 系统 | Windows 11 x64 | 10.0.26300 |
| 显卡 | NVIDIA，≥6 GB 显存 | RTX 3060 Laptop，6 GB |
| 驱动 | 支持 CUDA 12.x 的新驱动 | 已装 |
| WSL | WSL2 + Ubuntu 24.04 | 必须：肥鱼只能在 Linux 下跑（cu128 的 torch） |
| Python A | 3.13 → `.venv/` | 跑 ASR + bridge |
| Python B | 3.11 → `py311/` | 跑 CosyVoice worker |
| Ollama | 可选，本地兜底大脑 | `D:\ollama\ollama.exe`，监听 `127.0.0.1:11434` |
| 端口 | 8080（bridge）、11434（Ollama） | 被别的程序占用会起不来 |

显存预算（6 GB 卡的实测值）：

| 常驻 | 占用 |
|---|---|
| 肥鱼 worker（WSL） | ≈3.4 GB |
| SenseVoice（bridge 进程内） | ≈0.5 GB |
| Ollama qwen3.5:2b（切到 local 才加载） | ≈2.3 GB |
| 同时开肥鱼 + 本地大脑 | 5.7 / 6.1 GB，很挤，不建议长期这样跑 |

## 3. 目录分布

| 路径 | 内容 | 进不进备份 |
|---|---|---|
| `C:\Users\<你>\voice-ai\bridge\` | 桥、调试台页面、测试 | 必带 |
| `voice-ai\models\SenseVoiceSmall\` | ASR 模型 894 MB | 必带（或重新下） |
| `voice-ai\models\CosyVoice-300M-SFT\` | 二号声线 2.2 GB | 常用 |
| `voice-ai\CosyVoice\` | CosyVoice 源码 | 必带 |
| `voice-ai\py311\` | 3.11 运行时 | 建议带走（重装较烦） |
| `voice-ai\.venv\` | 3.13 运行时 | **不要跨机复制**，见 §6 |
| `voice-ai\firmware\` | ESP32 固件 | 必带 |
| `voice-ai\start-bridge.cmd` `start-console.cmd` | 启动脚本 | 必带 |
| `voice-ai\requirements.txt` `py311-requirements.txt` | 两个虚拟环境的包清单 | 必带 |
| `%USERPROFILE%\.voice-ai\brains.toml` | 大脑配置（云端/本地） | 必带（去掉密钥路径） |
| `%USERPROFILE%\.voice-ai\memory\` | **个人记忆数据** | 看你要不要带走 |
| `%USERPROFILE%\.voice-ai\console\turns.jsonl` | 调试台历史轮次 | 不建议（含对话原文） |
| `%USERPROFILE%\.voice-ai\usage.jsonl` | 用量账本 | 可选 |
| `%USERPROFILE%\.deepseek\key` `bridge_token` | **密钥** | 绝不进备份，目标机重建 |
| WSL `/root/feiyu-voice/` | 肥鱼模型 7.0 GB | 单独导出 |

## 4. 怎么启动

### 4.1 日常（推荐）

双击桌面 **「小鱼调试台」** → `start-console.cmd`，它会依次：

1. 检查 `11434`：没起就拉起 Ollama（路径 `D:\ollama\ollama.exe`，可用环境变量 `OLLAMA_EXE` 改）；
2. 清掉 WSL 里上次遗留的 `worker_feiyu.py`（不清理会占着 3.4 GB 显存，下次加载失败）；
3. 后台起 bridge（`start-bridge.cmd`，带记忆开关）；
4. 等 `8080` 起来；
5. 打开 Edge：`http://127.0.0.1:8080/console`。

冷启动时间：肥鱼 60 s（热）～195 s（冷）。页面上的 `engine` 胶囊变绿即可用。

### 4.2 手动

```bat
cd C:\Users\<你>\voice-ai
start-bridge.cmd              :: 默认 --serve，给设备用
start-bridge.cmd --mic --play :: 用本机麦克风和喇叭，不接设备
start-bridge.cmd --no-feiyu   :: 跳过肥鱼，用 CosyVoice
start-bridge.cmd --brain local:: 启动时直接选本地大脑
```

### 4.3 设备端（ESP32-S3 大肥鱼）

1. **供电**：两根 USB（供电 + 串口）都来自同一台 PC，共地；不要用 9V/12V 多档充电头。
2. **上电**：板子自动连 `secrets.h` 里的 Wi-Fi，进 IDLE。**复位时不要按住 BOOT**（会进下载模式）。
3. **说话**：IDLE 下按住 BOOT 开始录音，松开发送；静音 1.2 s 自动结束；最长 15 s；短于 0.3 s 丢弃。
4. **打断**：播报开始后按 BOOT 立即打断（固件为 `TURN_STOP` 模式才有）；等首字节那 2–4 s 里按键不响应。
5. **烧录/串口**：`arduino-cli`（在 D 盘，必须带 `--config-file`），串口多为 COM5、115200。细节与三种模式（RECORD_ONLY / TURN / TURN_STOP）见 `firmware/README.md`。

### 4.4 怎么关

- **调试台右上「关闭」按钮**：等价 `POST /shutdown`（只允许本机 + 需要 token），会打断当前轮、退出 bridge、并顺手 `pkill` WSL 里的肥鱼 worker。页面会盖一层「已关闭」。
- 直接关掉那个黑色 cmd 窗口也可以，但**不会**清 WSL worker，下次启动靠 `start-console.cmd` 兜底。
- 只是停一轮：点「打断」。

## 5. 配置

### 5.1 大脑（`~/.voice-ai/brains.toml`）

```toml
[brain]
active = "deepseek"        # 启动时用哪个；调试台里切换只在本次运行有效
fallback = []              # 备用链，按顺序

[brain.profiles.deepseek]
base_url = "https://api.deepseek.com"
key_file = "~/.deepseek/key"   # 只写路径，不写密钥本身
model = "deepseek-flash"
max_tokens = 320
timeout_s = 8
first_token_timeout_s = 3
extra_body = { thinking = { type = "disabled" } }

[brain.profiles.local]         # 断网兜底，免费
base_url = "http://127.0.0.1:11434/v1"
key_file = ""
model = "qwen3.5:2b-q4_K_M"
timeout_s = 45
extra_body = { reasoning_effort = "none" }   # 关思考只认这个，think=false 无效
```

实测：本地大脑热身后首字 ≈1.3 s、首段音频 ≈8 s（大头是肥鱼合成），适合兜底不适合日常。

### 5.2 环境变量（都有默认值，不设也能跑）

| 变量 | 默认 | 作用 |
|---|---|---|
| `MEMORY_ENABLED` / `MEMORY_DRY_RUN` | 0 / 1 | 五层记忆开关；`start-bridge.cmd` 设为 1 / 0 |
| `BRAIN_PROFILE` | 空 | 强制启动大脑，等价 `--brain` |
| `BRIDGE_PORT` | 8080 | 监听端口 |
| `FEIYU_ENABLED` | 1 | 是否启动肥鱼 |
| `FEIYU_MODEL_DIR` | `/root/feiyu-voice/model` | 肥鱼模型在 WSL 里的位置 |
| `FEIYU_STYLE` | `cute` | 肥鱼声线（F6） |
| `EDGE_VOICE` | `zh-CN-XiaoxuanNeural` | 在线兜底音色 |
| `COSYVOICE_FP16` | 0 | 实测无提速，保持 0 |

### 5.3 前端

`bridge/webui/console.html` 是单文件页面，无构建步骤，改完刷新浏览器即可。文件头的注释里列了它用到的全部接口和鉴权方式。

## 6. 打包带走

### 6.1 必带

```
voice-ai\bridge\            (代码 + 页面 + 测试)
voice-ai\models\            (两个模型)
voice-ai\CosyVoice\         (源码)
voice-ai\py311\             (3.11 运行时)
voice-ai\firmware\          (固件)
voice-ai\start-bridge.cmd
voice-ai\start-console.cmd
%USERPROFILE%\.voice-ai\brains.toml      (先把 key_file 路径改对)
```

### 6.2 目标机上要重建的

1. **虚拟环境**：`.venv` 里写死了本机路径，跨机不要复制，重建：
   ```bat
   cd voice-ai
   py -3.13 -m venv .venv
   .venv\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cu126 ^
       torch==2.14.1+cu126 torchaudio==2.11.0+cu126
   .venv\Scripts\python.exe -m pip install -r requirements.txt

   py311\python.exe -m pip install --index-url https://download.pytorch.org/whl/cu121 ^
       torch==2.3.1+cu121 torchaudio==2.3.1+cu121
   py311\python.exe -m pip install -r py311-requirements.txt
   ```
   `requirements.txt` / `py311-requirements.txt` 是 2026-10-28 用 `pip freeze` 从本机导出的（87 / 158 个包）。
2. **两个密钥文件**（内容自己填，别从旧机拷明文到云盘）：
   - `%USERPROFILE%\.deepseek\key`：DeepSeek API key
   - `%USERPROFILE%\.deepseek\bridge_token`：随便一串随机字符，页面和固件都用它
3. **WSL 侧**：装 Ubuntu 24.04，装 cu128 torch 与肥鱼依赖，把 `/root/feiyu-voice/`（7 GB）拷进去。路径与 `FEIYU_PY` / `FEIYU_MODEL_DIR` 要对得上。
4. **Ollama**（可选）：装好后 `ollama pull qwen3.5:2b-q4_K_M`。
5. **ESP32 固件**：拷 `secrets.h`（Wi-Fi + PC 的局域网 IP + bridge_token），`arduino-cli compile/upload`，端口多为 COM5。

### 6.3 绝对不要带

- `~/.deepseek/key`、`bridge_token` 的明文（会随备份上云）。
- `~/.voice-ai/console/turns.jsonl`（对话原文）。
- `~/.voice-ai/memory/`（个人记忆；确需迁移再单独拷，并当作敏感数据处理）。
- `bridge/logs/`（含录音相关日志）。

### 6.4 到新机器后的自检

```bat
:: 1) bridge 活着
curl -H "X-Auth: <token>" http://127.0.0.1:8080/api/state
:: 2) 三个引擎状态（feiyu.ready / edge.ready / cosyvoice）
::    看返回 JSON 里的 engine 字段
:: 3) 发一轮文字，应该听到声音
curl -H "X-Auth: <token>" -H "Content-Type: application/json" ^
     -d "{\"text\":\"你好\"}" http://127.0.0.1:8080/api/text > out.pcm
:: 4) 测试全绿
cd bridge && ..\.venv\Scripts\python.exe -m unittest test_console test_console_memory test_console_shutdown
```

## 7. 出问题先看这里

| 现象 | 先查 |
|---|---|
| 页面打不开 | `netstat -ano \| findstr :8080` 有没有监听；bridge 窗口里的报错 |
| 有文字没声音 | `/api/state` 里 `engine.feiyu.ready` 是否 true；不行看 `bridge/logs/feiyu.log` |
| 肥鱼加载失败 | WSL 里 `pgrep -af worker_feiyu.py` 是否有旧进程；`nvidia-smi` 显存是否被占 |
| 回复是空的 | 切到 local 时最容易：确认 `extra_body = { reasoning_effort = "none" }` |
| 局域网打不开录音 | 浏览器只在 `127.0.0.1` / `https` 下给麦克风，用 IP 访问录不了音 |
| 设备连不上 | 固件里的 PC IP 变了（DHCP）；`bridge_token` 和 PC 上不一致 |
| 底噪/杂音 | 地线是唯一主因，不要改音频参数（`CLIP_TARGET_RMS` / `CLIP_GAIN_MAX` 已定稿） |
