# voice_terminal — ESP32-S3 语音终端固件骨架（F 包）

本地语音陪伴项目的设备端。链路：

`ESP32-S3 录音 → Wi-Fi HTTP POST /turn → PC bridge（SenseVoice → 云端 LLM → TTS）→ chunked PCM → I2S 功放 → 喇叭`

**三模式都已上机验证（RECORD_ONLY 2026-10-26 定稿；TURN / TURN_STOP 2026-10-27 跑通）。** 接线、烧录、音频调参的全过程见 `handoff/G-hardware-bringup.md` 与根目录 `HANDOVER-G.md`；最终状态与文件清单见根目录 `HANDOFF.md`。除 `secrets.h` 的值和 `config.h` 里点名过的实测常量，其余代码保持冻结。

---

## 1. 硬件

### 1.1 板子

- ESP32-S3 DevKitC-1，**N16R8**：16 MB flash，8 MB **OPI PSRAM**。
- 烧录/串口走 **CH343 USB-UART**（需装 WCH CH343SER 驱动）；不要依赖原生 USB。

### 1.2 音频模块

鹿小班 **ICS43434 + MAX98357A** 集成模块（**自带喇叭的成品音箱板**）。

- 模块板载 HT33/HT7533 系列 3.3 V LDO **只给话筒**供电，MAX98357A 直接吃 **5 V**。
- **模块没有可插杜邦的 VCC / GND 针**：唯一的供电与地是它那根**一体注塑、掰不开的自带 USB 线**（红 = 5 V，黑 = GND）。旧记录里的 8 针（含 VCC）已作废。
- 因此模块与开发板之间**没有 GND 直连线**，共地只能“借道”：两路 USB **必须插在同一台电脑 / 同一个 USB hub / 同一个充电宝**，否则 I2S 失去公共参考（噪声或无声）。这是 2026-10-26 把沙沙调干净的关键，不要拆回两个独立电源。
- 音频线与地线都走短线；模块 USB 线不要延长。
- `SPK` 接喇叭；功放是 **BTL 输出，喇叭两端都不能接地**。

供电（文字图，当前实测形态）：

```
电脑或 hub 的 USB 口 A ── 模块自带 USB 线（红 5V / 黑 GND）
电脑或 hub 的 USB 口 B ── 板子 USB 线（CH343，兼作串口）
两个口来自同一台电脑 / 同一个 hub     ← 共地靠主板“借道”
```

### 1.3 接线（默认两路独立 I2S）

| 模块信号 | 接 ESP32-S3 | 说明 |
|---|---|---|
| SD（话筒数据） | GPIO6 | 话筒数据 |
| SCK（话筒位时钟） | GPIO4 | 话筒位时钟 |
| WS（话筒字选择） | GPIO5 | 话筒字选择 |
| DIN（功放数据） | GPIO7 | 功放数据 |
| BCLK（功放位时钟） | GPIO15 | 功放位时钟 |
| LRCLK（功放字选择） | GPIO16 | 功放字选择 |
| 供电 / 地 | 模块自带 USB 线 | 不接开发板任何引脚 |

- 默认按**两路独立 I2S**：RX 用 SD/SCK/WS，TX 用 DIN/BCLK/LRCLK。
- 2026-10-26 已实测模块内部**不共时钟**（7 组引脚全 open），所以两路各接各的。固件里的 `MIC_AMP_SHARED_CLOCKS` 默认 `0`；单总线路径没有实现，改成 1 会触发 `#error`。
- 禁用/避免：GPIO0/3/45/46（strapping，GPIO0 只给 BOOT 按键）、GPIO19/20（USB）、GPIO43/44（UART）、GPIO48（RGB）、GPIO35–37（octal PSRAM）。
- BOOT 按键用 GPIO0，低有效，内部上拉；**复位时不要按住**（会进下载模式）。

## 2. 编译环境

Arduino IDE 板子设置：

| 选项 | 值 |
|---|---|
| Board | ESP32S3 Dev Module |
| Flash Size | 16MB (128Mb) |
| PSRAM | **OPI PSRAM** |
| Partition Scheme | Default 4MB with spiffs（默认分区即可） |
| USB CDC On Boot | **Disabled**（串口走 CH343） |

- 目标 **Arduino-ESP32 core 3.x / ESP-IDF 5.x**，代码用 `driver/i2s_std.h` 的 `i2s_channel_*` API。core 2.x 编译不过，不要混用旧 `driver/i2s.h`。
- 除 Arduino-ESP32 自带组件外无第三方库依赖。

### 2.1 已做过的编译验证（最近一次 2026-10-27）

arduino-cli 1.5.1 + `esp32:esp32@3.3.12`，FQBN `esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,PartitionScheme=default,CDCOnBoot=default`：

| 模式 | `-DAPP_MODE` | Flash | RAM |
|---|---|---|---|
| RECORD_ONLY | 0（默认） | 397,319 B (30%) | 52,316 B (15%) |
| TURN | 1 | 943,302 B (71%) | 71,316 B (21%) |
| TURN_STOP | 2 | 943,486 B (71%) | 71,316 B (21%) |

- 2026-10-27 删掉开机音阶和 `playDC` / `playSweep` 两个未使用的诊断函数后重新编译，三模式均 **0 error / 0 warning**。
- 2026-10-27 早先在 10-26 音频重写之后也做过一次三模式编译（0 error，尺寸略大）。
- 2026-10-22 首次三模式编译同样 0 error（旧尺寸 377,943 / 935,654 / 935,838 B）。
- arduino-cli 用 `--build-property 'build.extra_flags=-DAPP_MODE=1'` 切模式；Arduino IDE 直接改 `config.h` 的默认值。
- 占位 `secrets.h` 只用于编译验证，已删除；现在设备上用的是用户自己填的**真实 `secrets.h`**（已 gitignore，任何人都不要读取或打印其中的值）。没有它时 TURN/TURN_STOP 会 `#error`。
- **联网两模式已上机（2026-10-27，COM5）**：TURN 多轮 `result=ok`（首字节 2.2–4.2 s，回复音频时长 1.3–55.7 s）；TURN_STOP 播放中按 BOOT 打断两次成功（`stop_ms=18/20 ms`，`result=aborted`）。
- **串口字段名有歧义，不要望文生义**：`upload_ms` 是**整轮墙钟**（上传+等待+播放+排空，`millis()-t0`）；`play_ms` 是**回复音频时长**（`pcm_bytes/32`，不是播放耗时）。真实播放耗时 ≈ `play_ms` + 32 ms × `underruns`。

#### 2026-10-27 上机时改掉的两处

1. **开机音阶已删**（原 6 级 × 2 s，约 16 s）。删掉后话筒从「每次 `min=0 max=0`」恢复为正常波形；机制尚未查清，只记录这个经验事实。开机到可录音现在约 2 s。
2. **播放缓冲 1 s → 3 s**。实测 bridge 的音频块大多在缓冲满时到达，断续来自句间 1.1–5.6 s 的合成停顿；1 s 缓冲会被抽干，3 s 能吸收中后段的停顿。剩余 underrun 集中在回复开头几秒（首次缓冲未填满），不再出现在中后段。
   干净版实测（无诊断打印）：55.7 s 的长回复 `underruns=46`（46×32 ms ≈ 1.47 s 静音，占 2.6%）；修之前 7.1 s 的回复就有 `underruns=59`（≈1.89 s，占 27%）。

烧录步骤：

1. 安装 WCH **CH343SER** 驱动，用 USB 线接开发板 CH343 串口（非原生 USB 口）。
2. 设备管理器确认出现新的 COM 口（CH343）。
3. 打开 `voice_terminal/voice_terminal.ino`，按上表设置板子选项，选择该 COM 口。
4. 如需联网模式：复制 `secrets.example.h` 为 `secrets.h` 并填入真实值（见第 4 节）。
5. 编译并上传。若一直进不了下载模式，按住 BOOT 再点上传，出现 `Connecting...` 后松开。

## 3. 三种模式

`config.h` 里的编译期常量（默认 `APP_MODE_RECORD_ONLY`）：

```c
#define APP_MODE APP_MODE_RECORD_ONLY  // 0=录音+串口统计+本地回放，不联网
                                       // 1=TURN：录音 + POST /turn + 播放回复
                                       // 2=TURN_STOP：再加 /stop 打断
```

**首次上电顺序**：先 `RECORD_ONLY` 录音回放（验证话筒、功放、喇叭、电源、引脚），再 `TURN`，最后 `TURN_STOP`。

操作：IDLE 下按住 BOOT 开始录音，松开结束；静音 1.2 s 自动结束；最长 15 s；短于 0.3 s 丢弃。播放中按 BOOT（仅 TURN_STOP）会**先本地 flush，再 POST `/stop`**；流结束后的本地排空阶段也能打断。录音那一次按键必须先松开，避免“静音/超时自动结束”被误判成新的打断按键。串口 115200 baud。

**打断时机（2026-10-27 定）**：只有音频开始播放之后按 BOOT 才生效。等待首字节（实测 2.2–4.2 s）期间按键不响应，也不会发 `/stop`。

## 4. 密钥

- 复制 `secrets.example.h` → `secrets.h`，填入真实 Wi-Fi / bridge_token。
- `secrets.h` 已在 `.gitignore` 中；不存在时联网模式编译会 `#error` 报错（默认 RECORD_ONLY 模式不需要它）。
- 真实 token 只存在设备本地：不打印、不上传、不写进 README。
- 固件里**没有** DeepSeek API key；设备只持有 bridge_token。

## 5. 接口契约（bridge 侧已实现，不要改）

- `POST /turn?session_id=dev1`，body 为完整 WAV（16000 Hz / 16 bit / mono），必带 `Content-Length = 44 + pcm_bytes`。
- 头：`X-Auth: <bridge_token>`、`Content-Type: audio/wav`。
- 响应：HTTP chunked PCM（s16le/16000/mono），头 `X-Voice: s16le/16000/mono`、`X-Gen-Id`、`X-Session-Id`。
- 空识别：HTTP 204；鉴权失败：HTTP 401。
- 打断：`POST /stop`，带 `X-Auth`，body 为空；全局打断。
- 无 TLS，仅局域网，`192.168.0.100:8080`。
- 设备端预缓冲 **160 ms（5120 bytes）** 才开 I2S TX；流缓冲 **3 s（98304 bytes）**，内部 RAM 不够时自动降到 2 s / 1 s（开机日志 `play_buf=` 打实际值）；`/stop` 后本地 flush。

## 6. 上机确认过的项与遗留

1. **24-bit 转换**：已由 2026-10-26 实测确认（方案 B：32-bit slot + `mic24_to_s16()` 右移 `MIC_SHIFT=16`；ICS43434 左对齐、左声道成立）。不要再按旧假设改移位。
2. **两路时钟**：已实测**不共**（7 组引脚全 open），按两路独立 I2S 接；`MIC_AMP_SHARED_CLOCKS=1` 仍会触发 `#error`，单总线路径未实现。
3. **VAD 阈值**：`REC_SILENCE_RMS` 以上机后的实际 `rms` 为准，当前值可用；改它要重测短句是否被丢。
4. **缓冲**：160 ms 起始水位 + 3 s 流缓冲已上机验证；长回复开头约 1 s 仍会补静音，根因在服务端合成节奏，继续加深缓冲没有收益（3 s 已接近内部 RAM 舒适上限）。
5. **按键时序**：TURN_STOP 的打断要求录音那一次按键先松开；若按住 BOOT 直到静音/15 s 自动结束，播放不会被同一次按键立即打断（防误触发的有意行为），松开后再按才会 `/stop`。已实测（`stop_ms` 18–20 ms）。
6. **开机音阶已删**：删之前录音偶发全零、删之后未再出现；机制未查清，别再把它加回去做“自检”。

## 7. 代码结构

```
voice_terminal/
  voice_terminal.ino   状态机、按键、录音/VAD、WAV 头、Wi-Fi
  config.h             全部可调常量（引脚/阈值/水位/模式/24-bit 假设）
  secrets.example.h    密钥模板（复制为 secrets.h）
  audio_io.h/.cpp      I2S RX/TX、mic24_to_s16、播放任务（FreeRTOS + 流缓冲）
  http_turn.h/.cpp     手写 HTTP/1.1 客户端：/turn、/stop、chunked 解析
```

实现要点：录音缓冲在 PSRAM（启动检查 `psramFound()`）；播放用 FreeRTOS 任务（优先级高于网络），网络任务写流缓冲、音频任务读；underrun 写静音并计数；chunked 自己解析（0 chunk 结束、忽略 `;ext`、逐 chunk CRLF、短读/超时保护）。
