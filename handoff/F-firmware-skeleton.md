# F 包 · ESP32-S3 语音终端固件骨架

> 把这份文件整段复制给负责 F 包的 AI。你只负责新建 `firmware/` 下的固件骨架，不要改 bridge、不要接线、不要采购。
> 如果用户说「停 / 停停」，立即停止当前操作，不要再写文件或运行命令。

## 1. 背景

这是一个本地语音陪伴项目的设备端。整条链路：

`ESP32-S3 录音 → Wi-Fi HTTP POST /turn → PC bridge（SenseVoice → 云端 LLM → TTS）→ chunked PCM → I2S 功放 → 喇叭`

PC 端已经跑通。现在需要设备端固件骨架，先在没有硬件的情况下把代码写好；硬件到货后由 G 包按检查点接线、烧录、调试。

## 2. 文件边界（只允许新建 `firmware/` 下的文件）

建议结构（可以拆更多新文件，但不要改 `firmware/` 之外的东西）：

```
C:\Users\DELL\voice-ai\firmware\
  README.md
  .gitignore
  voice_terminal\
    voice_terminal.ino
    config.h
    secrets.example.h
    http_turn.h
    http_turn.cpp
    audio_io.h
    audio_io.cpp
```

- **只新建**；不得修改、删除、重命名任何已有文件。
- 不要动 `bridge/config.py`、`llm.py`、`pipeline.py`、`server.py`、`tts.py`、`asr_engine.py`。
- 不要动 `HANDOFF.md`、`handoff/`、根目录 `README.md`。
- 不要创建真实的 `secrets.h`；只创建 `secrets.example.h`，让用户自己复制。
- 不要在代码、README、注释或日志里写真实 Wi-Fi 密码、bridge_token、API key。

## 3. 硬件

### 3.1 板子

- ESP32-S3 DevKitC-1，**N16R8**：16 MB flash，8 MB **OPI PSRAM**。
- 烧录/串口走 **CH343 USB-UART**（需要 WCH CH343SER 驱动）；不要依赖原生 USB。
- Arduino IDE 选 **ESP32S3 Dev Module**；Flash Size **16MB**；PSRAM **OPI PSRAM**；分区默认；USB CDC on boot 关闭（用 CH343 串口）。

### 3.2 音频模块

鹿小班 **ICS43434 + MAX98357A** 集成模块，含喇叭。

- 模块 VCC 是 **5 V 标称**；板载 HT33/HT7533 系列 3.3 V LDO 只给话筒，MAX98357A 直接吃 5 V。
- **外部 5 V 供电，和开发板共地。绝不能从开发板的 5V 引脚给模块供电。**
- 模块引脚从左到右：`SD、SCK、WS、DIN、BCLK、LRCLK、GND、VCC`。
- `SPK` 接喇叭；BTL 输出，**喇叭两端都不能接地**。

### 3.3 接线（默认两路独立 I2S）

| 模块引脚 | 接 ESP32-S3 | 说明 |
|---|---|---|
| SD | GPIO6 | 话筒数据 |
| SCK | GPIO4 | 话筒位时钟 |
| WS | GPIO5 | 话筒字选择 |
| DIN | GPIO7 | 功放数据 |
| BCLK | GPIO15 | 功放位时钟 |
| LRCLK | GPIO16 | 功放字选择 |
| GND | GND | 共地 |
| VCC | 外部 5 V | 不接开发板 5V 引脚 |

- 默认按**两路独立 I2S**写：RX 用 SD/SCK/WS，TX 用 DIN/BCLK/LRCLK。
- 到货后 G 会测 `SCK↔BCLK`、`WS↔LRCLK` 是否导通。若导通，说明模块内部共时钟，**不能**把两组时钟分别接到两个 GPIO 输出；必须改单总线方案并先报告。固件里留一个 `MIC_AMP_SHARED_CLOCKS` 常量，默认 `0`，但不要在未实测时改成 1。
- 禁用/避免：GPIO0/3/45/46（strapping，GPIO0 只给 BOOT 按键）、GPIO19/20（USB）、GPIO43/44（UART）、GPIO48（RGB）、GPIO35–37（octal PSRAM）。
- BOOT 按键用 GPIO0，低有效，内部上拉；**不要在复位时按住**（会进下载模式）。

## 4. 接口契约（不要改）

- `POST /turn?session_id=dev1`
- 请求体：完整 WAV，16000 Hz / 16 bit / mono；必须带 `Content-Length`。
- 头：`X-Auth: <bridge_token>`、`Content-Type: audio/wav`。
- 响应：HTTP chunked PCM，s16le / 16000 Hz / mono；头 `X-Voice: s16le/16000/mono`、`X-Gen-Id`、`X-Session-Id`。
- 空识别：HTTP 204，无音频。
- 鉴权失败：HTTP 401。
- 打断：`POST /stop`，带 `X-Auth`，body 可为空；全局打断。
- 无 TLS；只在局域网，地址 `192.168.0.100:8080`。
- 设备端先缓冲 **150–200 ms PCM** 再开 I2S（起始水位）；建议 `160 ms = 5120 bytes`。
- `/stop` 后设备**本地 flush**；服务端可能还要等当前句合成完才关连接，这不是可听延迟。

## 5. 必须实现的行为

### 5.1 三种模式（编译期常量）

```c
#define APP_MODE_RECORD_ONLY 0   // 录音 + 串口统计 + 本地回放，不联网
#define APP_MODE_TURN        1   // 录音 + POST /turn + 播放回复
#define APP_MODE_TURN_STOP   2   // 再加 /stop 打断

#ifndef APP_MODE
#define APP_MODE APP_MODE_RECORD_ONLY
#endif
```

- 默认 `APP_MODE_RECORD_ONLY`，给第一次上电测试用；G 会依次改成 1、2。
- `RECORD_ONLY` 不应要求 Wi-Fi；录完直接本地回放，用来验证话筒、功放、喇叭、电源、引脚。
- `TURN` / `TURN_STOP` 才连 Wi-Fi。

### 5.2 状态机

`IDLE → RECORDING → UPLOADING/PLAYING → IDLE`

- `IDLE`：按住 BOOT 开始录音。
- `RECORDING`：松开 BOOT 结束；或静音 1.2 s 自动结束；最长 15 s；短于 0.3 s 丢弃。
- `UPLOADING/PLAYING`：上传 WAV、解析 chunked PCM、预缓冲、播放。
- 播放中按 BOOT：`TURN_STOP` 模式下 POST `/stop`，本地 flush，回 `IDLE`；这一次按键不要同时开始录音。
- 任何错误都回 `IDLE`，串口打印原因；不要死循环、不要重启。
- 按键 30 ms 去抖；不要用长按/双击等复杂手势。

### 5.3 录音

- 16 kHz / 16 bit / mono。
- 录音缓冲放 **PSRAM**；最大 15 s = 480,000 bytes，建议分配 512 KB。
- 启动时检查 `psramFound()`；没有 PSRAM 就打印错误并停住，不要静默用内部 RAM。
- 用 `heap_caps_malloc(..., MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)`；打印 `ESP.getFreePsram()`。
- 生成标准 44 字节 WAV 头（RIFF/WAVE/fmt/data，PCM=1，1 声道，16000 Hz，16 bit）。
- 打印：录音毫秒、PCM 字节、RMS、peak。RMS/peak 用于 G 判断话筒是否有数据。
- 静音阈值、最短语音、最长录音、静音结束时间都做成 `config.h` 常量，方便 G 调。

### 5.4 ICS43434 的 24-bit 数据

话筒是 24-bit。两条实现路径，任选其一，但必须写清楚：

- **A**：RX 配 16-bit data width + 32-bit slot（如果所用 core 支持），直接取高 16 位；
- **B**：RX 配 32-bit data width，读 `int32_t`，再把 24-bit 转 `int16_t`。

要求：

- 封装成 `mic24_to_s16(int32_t raw)` 或等价函数。
- `config.h` 里留 `MIC_DATA_BITS`、`MIC_SHIFT`/`MIC_LEFT_JUSTIFIED` 等常量，并写注释说明假设。
- 串口打印**原始 min/max/RMS** 和转换后的 min/max/RMS。
- **不要在没有实测的情况下写死一个移位还声称正确。** G 会用串口数据确认；如果发现直流偏置/削顶/全零，按 G 的反馈只改 `firmware/` 里的常量或转换函数。

### 5.5 上传

- 只用 Wi-Fi 2.4 GHz（ESP32-S3 不支持 5 GHz）；`WiFi.setSleep(false)` 降低延迟。
- 连接超时 15 s；断线重连；打印 IP。
- 请求头必须含 `Content-Length`，值是 `44 + pcm_bytes`。
- 连接超时 5 s，读超时 20 s（首字节可能 5–9 s）。
- 建议直接用 `WiFiClient` 写一个小型 HTTP/1.1 客户端，自己解析状态行/头/chunked；**不要依赖 `HTTPClient` 对 chunked 的隐式处理**，除非你能在代码里明确证明它已解 chunk。
- 请求建议带 `Connection: close`，响应读到 0 chunk 后关闭。

### 5.6 响应解析

- 读状态行；非 200 时读掉/丢弃 body，打印前 200 字符，返回错误。
- 204：没有语音，回 `IDLE`。
- 401：token 错，打印明确提示，回 `IDLE`。
- 检查 `X-Voice` 必须是 `s16le/16000/mono`；不匹配就停止播放并报错。
- `Transfer-Encoding: chunked`：
  - 读一行十六进制 chunk size（可带 `;ext`，忽略扩展）；
  - size=0 表示结束，读 trailers 到空行；
  - 读满 size 字节，送播放缓冲；
  - 读 chunk 末尾 CRLF；
  - 处理超时/短读。
- 如果响应是 `Content-Length`（非 chunked），按长度读；两种都支持。
- 不要假设每次 `read` 都返回完整 chunk。

### 5.7 播放

- I2S TX：16000 Hz / 16 bit / mono / Philips；DIN=7，BCLK=15，LRCLK=16。
- 用 FreeRTOS 任务 + 环形/流缓冲；网络任务写，音频任务读。建议 `xStreamBufferCreate(32768, 1)`。
- **预缓冲 160 ms（5120 bytes）后才开 I2S TX**；如果整个回复短于 160 ms，在流结束时把已有数据播完。
- bridge 每块约 4096 bytes（128 ms）。如果解析器整块写入，缓冲会从 4096 直接跳到 8192（256 ms），超过 200 ms 水位；要按字节/小段喂入，达到 5120 就启动，目标 150–200 ms。
- underrun 时写静音（0），计数并打印；不要忙等、不要在音频任务里 `delay()`。
- 播放结束等缓冲排空，再停 TX；下一轮重新 enable。
- `/stop`：设置 abort、清空 stream buffer、disable/enable I2S TX 清 DMA，回 `IDLE`。
- 打印：首包毫秒、总 PCM 字节、播放毫秒、underrun 次数。

### 5.8 串口日志

115200 baud。每次状态变化打印一行，至少包含：

```
state=IDLE heap=... psram=...
record_ms=... bytes=... rms=... peak=...
wifi_ms=... ip=...
upload_ms=... status=... first_pcm_ms=... pcm_bytes=...
play_ms=... underruns=...
stop_ms=... result=...
```

不要打印 Wi-Fi 密码或 token。

## 6. 密钥与配置

`secrets.example.h` 只放占位符：

```c
#define WIFI_SSID "YOUR_2G4_SSID"
#define WIFI_PASS "YOUR_PASSWORD"
#define BRIDGE_HOST "192.168.0.100"
#define BRIDGE_PORT 8080
#define BRIDGE_TOKEN "YOUR_BRIDGE_TOKEN"
#define SESSION_ID "dev1"
```

- 用户自己复制成 `secrets.h` 并填值；`.gitignore` 里加 `secrets.h`。
- `secrets.h` 不存在时，编译应给出清晰错误（`#error`），不要用空 token 继续跑。
- 真实 token 只存在设备本地；不要打印、不要上传、不要写进 README。
- 固件里**没有** DeepSeek API key；设备只持有 bridge_token。

## 7. 代码要求

- 目标 **Arduino-ESP32 core 3.x / ESP-IDF 5.x**，用 `driver/i2s_std.h` 的 `i2s_channel_*` API。若本机只有 core 2.x，先报告，不要混用旧 `driver/i2s.h`。
- 不要用已废弃的 `i2s_read`/`i2s_write`，除非明确说明目标 core 2.x。
- 网络和音频分开任务；音频任务优先级高于网络任务；不要在网络回调里做长耗时操作。
- 不要用 `String` 拼接大块音频；PCM 用 `uint8_t*`/`std::vector` 的 PSRAM 缓冲。
- 不要用 TLS；不要引入额外库；尽量只用 Arduino-ESP32 自带组件。
- 不要实现 AEC、唤醒词、WebSocket、开机问候音；这一版是骨架。
- 不要改 bridge 契约。若发现必须加字段/端点，**停止并报告「需要 bb96 确认」**，不要直接改 server。

## 8. README 必须写清

- 引脚表、供电图（文字）、共地要求、绝不能接开发板 5V 引脚。
- 模块 VCC 5 V、HT33 只给话筒、功放直吃 5 V。
- 喇叭 BTL，不接地。
- 默认两路 I2S；到货先测 SCK↔BCLK、WS↔LRCLK；若导通改单总线。
- Arduino IDE 板子设置（ESP32S3 Dev Module、16MB、OPI PSRAM、默认分区、USB CDC 关）。
- CH343SER 驱动、COM 口、烧录步骤。
- 三种模式怎么切换、默认是 `RECORD_ONLY`。
- 首次上电顺序：先 `RECORD_ONLY` 录音回放，再 `TURN`，最后 `TURN_STOP`。
- 已知未验证项：24-bit 转换、两路时钟是否共、VAD 阈值、预缓冲是否够。
- 不要把真实 token/密码写进 README。

## 9. 验收

### 9.1 静态检查（必须做）

- 文件都在 `firmware/` 下；没有改 `firmware/` 外的任何文件。
- 没有真实 `secrets.h`；只有 `secrets.example.h`，值是 `YOUR_...` 占位符。
- 没有 `sk-` 开头的 key；没有真实 token/密码。
- 引脚常量与第 3.3 节一致；没有使用 GPIO19/20/43/44/48/35–37。
- WAV 头是 44 字节，`Content-Length = 44 + pcm_bytes`。
- chunked 解析处理 0 chunk、扩展、CRLF、短读。
- 预缓冲 160 ms；有 underrun 处理。
- `/stop` 有 POST、本地 flush、回 IDLE。
- PSRAM 检查；录音缓冲在 PSRAM。
- README 有供电/接线/模式/未验证项。

可以用这些命令辅助（在 Git Bash 下）：

```bash
find firmware -maxdepth 3 -type f -print
grep -RIn "sk-" firmware || true
grep -RIn "YOUR_" firmware
test -e firmware/voice_terminal/secrets.h && echo "ERROR: secrets.h must not exist" || true
```

### 9.2 编译（本机可能没有工具链）

先检查：

```bash
arduino-cli version
arduino-cli core list
```

- 如果 `arduino-cli` 不存在、Arduino IDE/ESP-IDF 也没有：**不要自行安装**，在报告里写「编译未验证：本机无工具链」，并给出 Arduino IDE 手动编译步骤。
- 如果工具链存在：先 `arduino-cli board details -b esp32:esp32:esp32s3` 看选项名，再用实际选项编译；不要照抄未验证的 FQBN。
- 如果编译失败，只改 `firmware/` 下的新文件；不要改 bridge。
- **没有编译输出，就不要写「编译通过」。**

### 9.3 人工审查

交付前自己逐条对照第 4、5、9.1 节；把未验证项列清楚。可以让另一个模型做交叉 review，但不要让它改 bridge。

## 10. 禁止事项

- 不要修改 bridge 或任何已有文件。
- 不要接线、不要采购、不要跑 GPU、不要下载模型。
- 不要写真实密钥/密码/token。
- 不要声称硬件已验证；没有硬件就没有验证。
- 不要实现 AEC、唤醒词、WebSocket、开机音、音乐播放、配网页面；这些不是本包。
- 不要用 `delay()` 做音频节拍；不要在音频任务里做 HTTP。
- 不要把 chunked 解析交给不确定的库行为；自己解析并写清。
- 不要把 24-bit 转换写死成未经验证的移位还当作事实。

## 11. 报告格式

```text
包名：F
模型/思考强度：
创建的文件：
确认未修改的已有文件：
编译状态：已编译 / 未验证（原因）
编译命令与输出尾部：
静态检查结果：
24-bit 转换方案（A/B + 假设）：
已知未验证项：
需要 G 在硬件上确认的 3 件事：
需要集成会话/bb96 处理的事：
```
