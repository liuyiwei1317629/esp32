# G 包 · 硬件到货后的接线与调试手册（2026-10-23）

> 把这份文件整段复制给负责 G 包的 AI。你是硬件调试助手：按检查点引导用户，不写业务代码、不改 bridge、不采购。
> **用户说「停 / 停停」立即停手。**

## 0. 什么时候开始

只有同时满足以下条件才开始：

1. 用户明确说「零件到了，开始接线」；
2. 模块（鹿小班 ICS43434 + MAX98357A）和电源线已到手；
3. 用户在场，能做插拔、按按键、看串口、拍照片。

截至 2026-10-23：**尚未收到用户到货确认**；模块预计 2026-10-23 到，USB-A→2P 杜邦电源线预计 2026-10-25 到；板子和杜邦线以用户实际清点为准。**不要提前假设已到货。**

### 0.1 为什么这套包默认不需要再改代码

- 默认路径（模块内部不共时钟）所需的一切都已在 `firmware/` 里编译通过；上机只允许改两类东西：
  1. `firmware/voice_terminal/secrets.h` 的**值**（Wi-Fi、bridge 地址、token）；
  2. `firmware/voice_terminal/config.h` 里本手册明确点名的**实测常量**（`MIC_SHIFT`、`REC_SILENCE_RMS`，必要时 `PLAY_PREBUFFER_BYTES` / `PLAY_STREAM_BYTES`）。
- 除这两类之外，任何代码改动都先停手报告；不改 `bridge/**`，不改 `/turn` 契约，不改防火墙。
- 唯一可能推翻“不改代码”的硬件分支：`SCK↔BCLK` 或 `WS↔LRCLK` 导通（模块内部共时钟）。此时**不是调常量能解决的**，必须停手、报告用户，按单总线方案重写固件；不要为了编译通过把 `MIC_AMP_SHARED_CLOCKS` 改成 1（会触发 `#error`）。
- 上机结束时，`config.h` 里实测改过的常量要写进报告；那是“实测值”，不是新的默认值。

### 0.2 最短路径（按顺序，不跳步）

```text
照片/型号 → 断电测短路与导通 → 空载测 5 V 极性 → 断电接线
→ 装 CH343SER 看 COM → RECORD_ONLY 烧录/录音回放
→ TURN 烧录/HTTP 一轮 → TURN_STOP 烧录/打断
→ 记录首字节与 underrun → 冻结、报告
```

## 1. 现有零件与硬约束

| 件 | 说明 |
|---|---|
| ESP32-S3 N16R8 开发板 | 16 MB flash，8 MB **OPI PSRAM**，CH343 USB-UART |
| 40P F-F 杜邦线 21 cm | 接线用 |
| 鹿小班 ICS43434 + MAX98357A 模块 | 含喇叭；VCC **5 V** |
| USB-A → 2P 母杜邦 300 mm 24 AWG | 模块电源；红 VCC / 黑 GND（到货先测） |
| 白色 USB-A→USB-C 6A 线 | 给板子供电/烧录；可能只充电，必须用 COM 口验证 |
| J-120W 充电器 | 标 5V3A/9V2A/11V1.5A/12V1.5A，实际最大 18 W；“120W”是型号名 |

硬约束：

- **模块 VCC 5 V 标称**；板载 HT33/HT7533 系列 3.3 V LDO 只给话筒，MAX98357A 直接吃 5 V。不要用 3.3 V 当设计电压。
- **外部 5 V 给模块，和开发板共地。绝不能从开发板的 5V 引脚给模块供电。**
- 模块引脚从左到右：`SD、SCK、WS、DIN、BCLK、LRCLK、GND、VCC`；`SPK` 接喇叭。
- 喇叭是 BTL 输出，**两端都不能接地**。
- 默认按**两路独立 I2S**：话筒 SD/SCK/WS，功放 DIN/BCLK/LRCLK。到货先测 `SCK↔BCLK`、`WS↔LRCLK` 是否导通；若导通，必须停手报告，改单总线方案。固件把 `MIC_AMP_SHARED_CLOCKS=1` 做成了编译期 `#error`：不要为了编译通过把它改回 0 再强行按两路接，两个时钟输出会打架。
- 不要重复采购；不要剪白色 6A 线；不要改 bridge/防火墙/契约。
- **不要跑 GPU。** 前两阶段用 `--no-cosyvoice`，只用 edge-tts + SenseVoice(CPU)。

## 2. 工具

- 万用表（通断/直流电压/电阻）。
- 小十字螺丝刀/镊子（如果需要）。
- PC（Windows 11，Git Bash）、USB 线、充电器。
- Arduino IDE（推荐）或 `arduino-cli`；CH343SER 驱动。
- 手机/相机拍丝印、引脚、读数、接线。
- 可选：USB 电流表（没有也能做，靠电压和现象判断）。

## 3. 检查点 0：照片与型号确认

先让用户拍这些照片，不要凭文字猜：

1. 开发板正面/背面丝印，确认 **N16R8**（或 N8R8；不能是 N8R2）。
2. 模块正面/背面，确认引脚丝印是 `SD、SCK、WS、DIN、BCLK、LRCLK、GND、VCC`。
3. 模块电源输入附近是否有 HT33/HT7533 字样；确认 VCC 是 5 V 设计。
4. 喇叭背面阻抗（4Ω/8Ω）和接线端子。
5. 电源线两端：USB-A 与 2P 杜邦，红/黑标识。
6. 充电器铭牌（5 V 档）。
7. 白色 6A 线的两端（USB-A/USB-C）。

型号不对/丝印看不清/模块不是 5 V → **停**，不要接线，先报告。

## 4. 检查点 1：PC 侧预检（不接硬件）

在 `C:\Users\DELL\voice-ai` 下：

```bash
# 现有单测：无网络、无 GPU
.venv/Scripts/python.exe bridge/test_sentence_split.py
.venv/Scripts/python.exe bridge/test_llm.py
.venv/Scripts/python.exe bridge/test_pipeline.py

# 记忆阶段 1–5：200 例，无网络、无 GPU、不读写真实记忆目录
cd bridge
../.venv/Scripts/python.exe -m unittest -v \
  test_memory_session test_memory_store test_memory_extract test_memory_score \
  test_memory_context test_memory_maintenance test_memory_admin test_memory_writer \
  test_memory_integration
cd ..
```

- 固件上机期间保持 **`MEMORY_ENABLED=0`**（默认），不要为了“顺便测记忆”打开开关；即使打开，默认 `MEMORY_DRY_RUN=1` 也不落盘，反而多一个变量。
- 记忆测试使用临时目录，不会创建 `~/.voice-ai/memory/`；若测试后出现该目录，说明有人改了测试或环境，先报告。

端到端（假 LLM + 真 ASR + edge，不占 GPU）：

```bash
# test_e2e.py 不发 X-Auth；本机已有 bridge_token 时会 401。
# 用不存在的 token 文件跑 127.0.0.1:8088，不要改测试代码。
BRIDGE_HOST=127.0.0.1 BRIDGE_PORT=8088 \
BRIDGE_TOKEN= BRIDGE_TOKEN_FILE=C:/Users/DELL/no-such-token \
  .venv/Scripts/python.exe bridge/test_e2e.py
```

这会生成 `bridge/out/in_16k.wav`，供下一步本地 `/turn` 测试用。

启动桥（只用 edge，不占 GPU）：

```bash
.venv/Scripts/python.exe bridge/server.py --serve --no-cosyvoice
```

等看到 `bridge listening` 和 SenseVoice 加载完（约 9–15 s）。另开一个终端，做本机 `/turn`：

```bash
TOKEN=$(cat ~/.deepseek/bridge_token)
curl -sS -D /tmp/headers.txt -X POST \
  "http://127.0.0.1:8080/turn?session_id=pc" \
  -H "X-Auth: $TOKEN" \
  -H "Content-Type: audio/wav" \
  --data-binary @bridge/out/in_16k.wav \
  -o /tmp/reply.pcm
wc -c /tmp/reply.pcm
grep -i "X-Voice\|X-Gen-Id" /tmp/headers.txt
```

- `/tmp/reply.pcm` 字节数应 > 0；`X-Voice` 应为 `s16le/16000/mono`。
- **不要把 token 打印出来**；`TOKEN=$(cat ...)` 不会显示。
- 本机 curl 成功，说明 PC 桥、ASR、edge、chunked 都正常；设备侧失败就不是桥的问题。
- 如果 8080 已被占用，不要杀未知进程；先问用户是不是已经有一个桥在跑。可以换端口测试，但设备契约默认 8080。

## 5. 检查点 2：断电测量

**全程断电，模块不接电。**

### 5.1 模块 VCC–GND 是否短路

- 万用表电阻档/通断档，测模块 VCC 与 GND。
- 正常：不是 0 Ω；电容充电会有瞬时摆动。
- 若稳定 < 10 Ω 或持续蜂鸣 → **停**，模块可能短路，不要上电。

### 5.2 时钟是否内部相连

- 测 `SCK ↔ BCLK`：蜂鸣/接近 0 Ω = 内部相连；开路 = 独立。
- 测 `WS ↔ LRCLK`：同上。
- 记录四种结果。若任一对导通 → **停**，报告，改单总线方案；不要按默认两路接。`config.h` 里的 `MIC_AMP_SHARED_CLOCKS` 只能由改代码的人打开，G 不要改。
- 若都开路 → 按两路独立接。

### 5.3 喇叭

- 测 `SPK+ ↔ SPK-`：应有 4–8 Ω 左右（喇叭阻抗）。
- 测任一端对 GND：应开路；若导通 → **停**，BTL 不能接地。
- 喇叭线不要短路到模块其他引脚。

### 5.4 杜邦线

- 每根线两端通断；标记颜色/编号，避免接错。
- 电源线红/黑极性先用万用表确认；卖家说红=VCC、黑=GND，但以实测为准。

## 6. 检查点 3：上电测电压（不接板子）

1. 充电器只接电源线，不接模块；万用表测 2P 杜邦输出。
2. 目标：**5.0–5.2 V**。
3. 若读到 3.3 V、9 V、12 V 或 0 V → **停**，不要接模块。确认用的是 5 V USB-A 口，不是 PD 9V/12V 档。
4. 确认红=VCC、黑=GND；若反了 → **停**，不要插。
5. 断电，准备接线。

## 7. 检查点 4：接线

断电操作。按默认两路独立 I2S：

| 模块 | 开发板 |
|---|---|
| SD | GPIO6 |
| SCK | GPIO4 |
| WS | GPIO5 |
| DIN | GPIO7 |
| BCLK | GPIO15 |
| LRCLK | GPIO16 |
| GND | GND |
| VCC | 外部 5 V（不接板子 5V 引脚） |

- 先接 GND，再接信号，最后接 VCC。
- 电源线红→模块 VCC，黑→模块 GND；板子与模块共地。
- 板子用白色 6A 线接 PC 的 CH343/UART 口供电+烧录；模块用外部 5 V。
- 不要把杜邦线插到 GPIO19/20、43/44、48、35–37；不要用 GPIO0 以外的方式接 BOOT。
- 接完再拍一张全景照片；逐根核对，不要凭记忆。
- 上电前再测一次模块 VCC–GND 不短路。

## 8. 检查点 5：COM 口 / 烧录

1. 装 **WCH CH343SER** 驱动（不是 CH340；如果板子丝印/设备管理器显示 CH340C，才用 CH340 驱动）。
2. 白色 6A 线接 CH343/UART 口（不是原生 USB 口）。
3. 设备管理器出现 COM 口 → 数据线通过；没有 COM 口 → 换一根确认能传数据的线，或报告线材问题。不要剪线。
4. 板子选项（与已验证的编译参数一致）：ESP32S3 Dev Module、**16MB Flash**、**OPI PSRAM**、默认分区、**USB CDC On Boot 关闭**。用 Arduino IDE 时照此选；用 arduino-cli 时下面的 FQBN 已包含这些选项。
5. 先确认 `firmware/voice_terminal/config.h` 的 `APP_MODE` 是 `APP_MODE_RECORD_ONLY`（0）。RECORD_ONLY **不需要** `secrets.h`。
6. 编译 + 上传（在 `C:\Users\DELL\voice-ai` 下；`COMx` 换成设备管理器里的 CH343 口）：

```bash
# 只编译 RECORD_ONLY
D:/arduino-cli/arduino-cli.exe --config-file D:/arduino-cli/arduino-cli.yaml compile \
  --fqbn 'esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,PartitionScheme=default,CDCOnBoot=default' \
  --build-property 'build.extra_flags=-DAPP_MODE=0' \
  --output-dir D:/arduino-build/g-record \
  firmware/voice_terminal

# 编译并上传（推荐一步完成）
D:/arduino-cli/arduino-cli.exe --config-file D:/arduino-cli/arduino-cli.yaml compile --upload -p COMx \
  --fqbn 'esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,PartitionScheme=default,CDCOnBoot=default' \
  --build-property 'build.extra_flags=-DAPP_MODE=0' \
  --output-dir D:/arduino-build/g-record \
  firmware/voice_terminal
```

- 若上传失败：按住 BOOT → 点 RST → 松开 BOOT，再上传；这是下载模式，不是正常运行。仍失败就停，先查驱动/线/板型，不要反复盲刷。
- 可选核对宏：`grep -o 'DAPP_MODE=[0-9]' D:/arduino-build/g-record/compile_commands.json | head -1`。
7. 串口监视器 115200：

```bash
D:/arduino-cli/arduino-cli.exe --config-file D:/arduino-cli/arduino-cli.yaml monitor -p COMx -c baudrate=115200
```

看到 `state=IDLE` 和 PSRAM/heap 日志。没有 COM 口 → 回到第 1–3 步，不要剪线。

### 8.1 切到 TURN / TURN_STOP

- `TURN` 需要 `secrets.h`：在 `firmware/voice_terminal/` 下从 `secrets.example.h` 复制为 `secrets.h`，由**用户本人**填 Wi-Fi（2.4 GHz）、`BRIDGE_HOST`、`BRIDGE_PORT`、`BRIDGE_TOKEN`、`SESSION_ID=dev1`。不要把 token 贴进对话、文档或截图；`secrets.h` 已被 `.gitignore` 忽略。
- 改编译参数即可，不要改源码逻辑：
  - TURN：`-DAPP_MODE=1`
  - TURN_STOP：`-DAPP_MODE=2`
- 每次切换都重新 `compile --upload -p COMx`，输出目录换成 `D:/arduino-build/g-turn` / `g-turn-stop`，避免旧二进制混用。
- `config.h` 里的 `APP_MODE` 默认值可以不改；命令行 `-DAPP_MODE` 会覆盖它。用 Arduino IDE 时才需要手改默认值。

## 9. 检查点 6：录音 + 本地回放（不联网）

1. 按住 BOOT，说一句 2–4 秒的话（例如「你好，测试一下」），松开。
2. 串口应打印 `record_ms`、`bytes`、`rms`、`peak`。
3. 模块喇叭应回放刚才的录音。
4. 判定：
   - `bytes` 约等于 `record_ms × 32`（16k × 2 字节/样本 × 1 声道）。
   - `rms`/`peak` 不应全 0；正常说话 peak 通常明显大于底噪。
   - 回放声音清楚、无严重削顶/噪声。
5. 失败分支：
   - 全 0 / 极小：查话筒 5 V、SCK/WS/SD、24-bit 转换、声道选择；让用户把 `record_ms/bytes/rms/peak` 贴回来。
   - 有数据但回放无声：查功放 5 V、DIN/BCLK/LRCLK、喇叭、BTL 不接地；查 `X-Voice` 不适用（还没联网）。
   - 声音失真/啸叫：查采样率、24-bit 移位、供电、喇叭接触；不要先改 VAD。
6. 通过后再进入下一阶段。

## 10. 检查点 7：HTTP 一轮

1. 确认 PC 的 LAN IP 仍是 `192.168.0.100`（`ipconfig`）。若变了，改 `secrets.h` 的 `BRIDGE_HOST`，不要改 bridge。
2. Wi-Fi 必须是 **2.4 GHz**；ESP32-S3 不支持 5 GHz。
3. 用 `-DAPP_MODE=1` 重新编译/上传（见 8.1），不要改源码逻辑；`secrets.h` 必须已由用户填好。
4. 串口应打印 Wi-Fi 连接、IP、`state=IDLE`。
5. 按住 BOOT 说「你好」，松开。
6. 串口应打印 `status=200`、`first_pcm_ms`、`pcm_bytes`，喇叭播放回复。
7. 判定：
   - 401 → bridge_token 不一致；检查 `secrets.h`，不要打印 token。
   - 204 → ASR 空；回到检查点 6 看 RMS/录音。
   - 连接失败/超时 → 桥没起、IP 错、防火墙 8080、不同网段；报告用户，不要改防火墙。
   - 200 但无声 → 查 `X-Voice`、chunked 解析、预缓冲、I2S TX。
8. 记录首字节（`first_pcm_ms`）和桥端 `FIRST_AUDIO_MS`；两者应接近。

## 11. 检查点 8：`/stop` 打断

1. 用 `-DAPP_MODE=2` 重新编译/上传（见 8.1）。
2. 发起一轮较长回复（例如问「给我讲一段长一点的话」）。
3. 先确认录音那一次按键已经松开（静音/15 s 超时自动结束的轮次同样要先松手），再在播放中按一下 BOOT。固件要求「松手后才能用同一次按键打断」，避免把录音结束误判成打断。
4. 串口应打印 `/stop` 请求和 `stop_ms`；喇叭应在本地 `playerAbort()` 后**立刻**安静，`/stop` 的网络往返不应拖住静音。流结束后的本地排空阶段也必须能这样打断。
5. 服务端连接可能晚几秒关闭（在途句合成无法取消），这不是失败；设备端应已停。
6. 失败：查 `/stop` 的 `X-Auth`、Content-Length: 0、abort flag、I2S disable/enable 清 DMA；若上行 401，固件会先检测服务器提前响应，不应再报 upload broken。

## 12. 检查点 9：延迟与调参

记录一轮干净数据：

```text
ASR（桥日志）：
首句字数：
首字节（设备 first_pcm_ms）：
首字节（桥 FIRST_AUDIO_MS）：
后续句是否跟得上：
underruns：
```

- 首字节目标先看 3 s 以内；7–8 字首句 + 快态 CosyVoice 约 2.1–2.4 s。
- 若首字节 > 5 s：先确认没有别的 GPU 会话占卡；CosyVoice 慢态重启恢复，不要换引擎。
- 若 underrun：把预缓冲从 160 ms 调到 200 ms，或增大 stream buffer；只改 `firmware/` 常量，不改 bridge。
- 若录音提前截断：调 VAD 静音阈值/结束时间；先看 RMS 日志，不要盲调。
- 若麦克风太小/太大：先确认 24-bit 转换，再考虑硬件增益；不要用软件放大掩盖削顶。

## 13. 失败矩阵

| 现象 | 先查 | 不要做 |
|---|---|---|
| 没有 COM 口 | CH343SER 驱动、UART 口、数据线 | 不要剪线、不要改 bridge |
| 上传失败 | BOOT+RST 下载模式、板子型号、USB 口 | 不要反复盲刷 |
| 没有 Wi-Fi | 2.4 GHz、SSID/密码、路由器隔离 | 不要改用 5 GHz |
| 401 | token 文件与 `secrets.h` 一致性 | 不要把 token 贴进对话 |
| 204 | 录音 RMS、话筒接线、24-bit | 不要改 ASR |
| 连接超时 | 桥是否在跑、IP、8080 防火墙 | 不要改防火墙；报告用户 |
| 有 PCM 无声 | I2S TX、5 V、喇叭、BTL | 不要把喇叭端接地 |
| 话筒全 0 | 5 V、SCK/WS/SD、声道、24-bit | 不要静默猜移位 |
| 失真/削顶 | 24-bit 转换、采样率、供电 | 不要用软件增益掩盖 |
| 重启/掉电 | 5 V 供电、共地、短路、strapping | 不要继续带电插拔 |
| 首字节很慢 | 是否有别的 GPU 任务、CosyVoice 慢态 | 不要换 TTS 引擎 |

## 14. 安全与边界

- 本 runbook 不改 `bridge/`；契约变更先问用户。
- 不改 Windows 防火墙 8080 入站规则；连接失败只报告。
- 不跑 GPU；`--no-cosyvoice` 起步；要跑 CosyVoice 必须用户明确同意且没有别的会话占卡。
- 不采购；零件不够就停。
- 不带电插拔模块；先断电再改线。
- 不把 token、密码、录音、完整日志上传到公开地方。
- 用户说「停/停停」立即停止所有操作。

## 15. 报告格式

每个检查点贴：

```text
检查点 | 操作 | 证据（照片/读数/串口） | 通过?
供电电压（空载）：
VCC-GND 是否短路：
SCK-BCLK 导通：
WS-LRCLK 导通：
喇叭阻抗 / 对地：
COM 口：
录音 bytes/rms/peak：
本地回放：
HTTP status / first_pcm_ms / pcm_bytes：
/stop stop_ms：
underruns：
当前 APP_MODE：
MEMORY_ENABLED（应为 0）：
改过的 config.h 常量（没有就写“无”）：
固件源文件是否除 config 常量外改动（应为“无”）：
PC LAN IP / 桥启动方式（--no-cosyvoice）：
下一步：
```

不要贴 token、密码、完整录音。

## 16. 收口条件（全部满足才算 G 完成）

- 检查点 0–8 全部有证据；检查点 9 至少有一轮干净数据（ASR、首句字数、设备首字节、桥 `FIRST_AUDIO_MS`、underrun）。
- RECORD_ONLY 的录音 `bytes`、`rms`、`peak` 合理，本地回放清楚；TURN 的 `status=200`、`pcm_bytes > 0`、喇叭有声；TURN_STOP 的本地静音不依赖网络往返。
- 没有为了通过而改契约、改 bridge、改防火墙、改固件逻辑；只允许 `secrets.h` 值与 `config.h` 实测常量变化。
- 若共享时钟导通、模块 VCC 不是 5 V、板子不是 N16R8/N8R8、供电异常或出现冒烟/异味：**停手报告，不算完成**。
- 完成后把“实测常量 + 证据 + 未验证项”写回报告；不要把上机调过的常量静默当成默认值。
