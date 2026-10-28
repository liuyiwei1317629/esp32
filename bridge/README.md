# voice bridge — ESP32 ↔ 本机 ↔ DeepSeek

一轮对话的链路：`ESP32 录音 → POST /turn → SenseVoice → DeepSeek 流式 → 按句切分 → TTS → chunked PCM → ESP32`
三个推理环节**并发**：第 k 句在播放时，k+1 正在合成，k+2 正在生成。

## 进程布局（本机三个 Python 环境，零新增安装）

| 进程 | 解释器 | 职责 |
|---|---|---|
| `server.py` | `.venv` py3.13 | HTTP 桥、SenseVoice 常驻、调度、计时 |
| `worker_cosyvoice.py` | `py311` | 主线 TTS，常驻复用（加载 15–20 s 只付一次，含一句自预热） |
| `worker_edge.py` | 系统 py3.13 | 兜底 TTS（edge-tts 7.3.1 + imageio-ffmpeg） |

worker 之间是 stdin/stdout 的 JSON-lines，PCM 用 base64 传递。日志在 `logs/`。

**协议必须是字节级 UTF-8**：Windows 上继承的 stdout 是 GBK，`sys.stdout.write` 中文会写成 GBK 字节，父进程按 UTF-8 解码就成了乱码（反向同理）。这条一旦坏了：CosyVoice 认不出说话人「中文女」，每句都静默回落 edge；送进 TTS 的文本本身是乱码，声音照样"非静音"，只有把音频喂回 ASR 才看得出来。

## 接口契约

- 监听 `0.0.0.0:8080`（本机 LAN IP `192.168.0.100`）
- `POST /turn?session_id=<id>`，请求体 = **16000 Hz / 16 bit / mono WAV**
- 响应 = **PCM s16le / 16000 Hz / mono，HTTP chunked**；头 `X-Voice: s16le/16000/mono`、`X-Gen-Id`
- `POST /stop` 打断当前代次（barge-in）
- 屏幕（2026-10-28 新增，**默认关闭**，见下面「屏幕推流」）：
  - `GET /screen` 一条长连接，返回 `multipart/x-mixed-replace; boundary=frame`，每 part 一帧 320×240 JPEG；**不用 chunked**，连接关闭即结束；要 `X-Auth`（无 token 401），`SCREEN_ENABLED=0` 时 503
  - `POST /screen/key` 设备按键上报（空 body），回 `{"state":"...","anim":"..."}`；同样要 `X-Auth`
- `X-Auth` 头带共享 token（已配置并实测：无 token 的请求 401）；**API key 只留在本机，绝不下发到设备**
- 调试台端点（2026-10-27 新增，**纯增量，上面几条一个字节没改**）：
  - `GET /console` 单页，免鉴权；页面本身不含数据，从本机访问时服务端把 token 注入进去
  - `GET /api/state` brain active/pinned/cooling / profiles / profile_meta（模型名与价格，**不含密钥**）/ engine / memory / 关键 config
  - `GET /api/turns?since=N&log_since=M` 增量拉轮次快照与日志行，返回新游标
  - `GET /api/memory[?text=1]` 默认只回计数，`text=1` 才回记忆原文
  - `GET /api/usage`（2026-10-28 新增）：只读汇总 `~/.voice-ai/usage.jsonl`，回今日/本月/累计金额、轮数、未计价轮数、按大脑拆分；日/月界按本机时区算。**只看账，不设上限、不拦调用**。调试台顶栏「花费」胶囊就是它
  - `GET /api/songs`、`POST /api/songs?title=&name=`、`POST /api/songs/delete`（2026-10-28 新增，loopback-only）：看歌单 / 加歌 / 删歌。加歌 body 是**原始音频字节**，`name` 是原文件名（决定格式），`title` 留空就用文件名；服务端转成 16 kHz 单声道 s16le 存进 `songs/`，只收 wav/mp3/flac/ogg，≤32 MB、≤10 分钟，超过回 400/413。删除 body `{"file":"x.pcm"}`，文件也一起删。歌单每轮现读，加/删**立刻生效、不用重启**。调试台「歌曲」按钮是它的 UI
  - `POST /api/text` body `{"text":"..."}`，跳 ASR，其余走同一条流水线
  - `POST /shutdown` / `POST /restart`（2026-10-28 新增，loopback-only）：关闭 / 重启本机 bridge。重启会先拉一个分离的等待进程（`bridge/relaunch.py`），等旧进程连 worker 一起退出、端口让出来后，再按桌面的 `start-bridge.cmd` 开一个新控制台窗口；旧进程退出前先回 200，页面自动重连
  - `POST /api/volume`（2026-10-28 新增，loopback-only）：body `{"level":30|50|70|100}`，bridge 在发 PCM 前按档位衰减（原唱和 TTS 都走这条路），档位存 `~/.voice-ai/playback.json`；固件增益不动，所以只影响响度、不改音色
  - `GET /api/state` 里的 `volume` 字段回报当前档位和可选档位
  - 切 API 复用原有 `POST /brain`（body `{"profile":"<name>"}`，LAN-only）；调试台只是把它的 UI 做出来了
  - 这一组都要 `X-Auth`；`/turn` 的行为、响应头、PCM 分块完全不变
  - 轮次落盘 `~/.voice-ai/console/turns.jsonl`（个人数据，不进仓库）；`CONSOLE_RECORD=0` 关闭

两个 TTS 源的原始采样率是 22050（CosyVoice）和 24000（edge-tts），**都在 worker 内重采样到 16k**，设备侧只管一种格式。

## 跑

```
# 需要先有 DeepSeek key（见下）
.venv/Scripts/python.exe bridge/server.py --serve

# 不占 GPU、不等 key 的自检：假 LLM + 真 ASR + 真 edge-tts 走完整 HTTP 链路
.venv/Scripts/python.exe bridge/test_e2e.py

# 真实全链（要 key，两轮真实 DeepSeek 调用）：真 ASR + 真 LLM + 真 CosyVoice + 真 barge-in
.venv/Scripts/python.exe bridge/test_live.py

# 本机听效果（文本进，扬声器出，打印分环节耗时）
.venv/Scripts/python.exe bridge/server.py --text "今天好累啊" --play --no-cosyvoice
```

单元测试：`test_sentence_split.py`（10 项切分规则）、`test_llm.py`（9 项 SSE 解析）、`test_pipeline.py`（10 项并发、打断、本地命令与应急兜底）。这三个是**脚本式**自检（直接 `python test_x.py` 跑，导入即执行），不要放进 `unittest` 清单——它们会在导入时改写 `llm.stream_reply` / `local_plugin.stream_reply` 并污染同进程的其他模块。其余 `unittest` 模块（2026-10-27 新增 `test_console.py` 24 例，2026-10-28 新增 `test_console_restart.py` 9 例、`test_playback.py` 9 例、`test_console_volume.py` 5 例、屏幕 6 模块 35 例；`test_songs.py` 由 6 例增至 11 例）用显式清单跑，共 **35 模块 528 例**，见 `HANDOFF.md` §14.3。屏幕那 6 个**只能在 Windows 本机跑**（要 PIL、微软雅黑和 20 MB 帧库），不要加进 `hermetic.yml`。

## 调试台（2026-10-27）

浏览器打开 `http://127.0.0.1:8080/console`。本机访问时 token 自动注入页面；从局域网 IP 打开要手填 `~/.deepseek/bridge_token`。

- **发一轮**：直接打字（跳 ASR，走 `POST /api/text`）或按住说话（浏览器采 16k WAV，走原 `POST /turn`）
- **首字节瀑布图**：ASR → LLM 首字 → 凑满首句 → 合成首句 → 送达，一眼看出卡在哪段
- **本轮卡片**：ASR 原文、回复全文、引擎/模型/字数/tokens、记忆五层装配明细（L1/L2/L3/L4 + token 预算 + 被裁剪的层）、原始 JSON
- **历史**：右栏按轮次列出首字节耗时，红黄绿分级；点开看详情；`#turn=N` 可直达某一轮
- **实时日志**：把 stdout 的 `report()` 输出推到页面上，页面自动滚底
- **session 桶**：`web`（不污染设备记忆）/ `dev1`（与设备共用），选择记在 localStorage
- **记忆按钮**：显式请求 `?text=1` 才回记忆原文，默认只给计数
- **切 API**：点顶部 `brain` 胶囊展开下拉，列出 `~/.voice-ai/brains.toml` 里所有 profile（带模型名、价格、冷却状态），点一下即 `POST /brain`，**下一轮生效**，不用重启。下拉里的 profile 数量 = brains.toml 里启用的数量；新增供应商要在那个文件里取消注释并配 `key_file`
- **一键重启 / 关闭**（2026-10-28）：顶栏「重启」= 停旧 bridge（含肥鱼 worker）→ 等端口让出 → 新开一个「小鱼调试台」命令行窗口 → 页面自动刷新；「关闭」只退出，不重开。两者都只允许本机页面调用
- **音量下拉**（2026-10-28）：顶栏「音量 小/中/大/满」= 30/50/70/100%，改完立刻生效，板子和页面试听同时变小；页面刷新后从 `/api/state` 读回当前档位

设计约束两条：一是所有调试挂钩都包了 `try`，调试台挂掉不影响一轮真实对话；二是 `build_report()` 的输出与历史 `report()` 逐字一致，不会改变既有的日志阅读习惯和脚本解析。

代码：`console_hub.py`（环形缓冲 + 游标 + 落盘）、`webui/console.html`（单页，原生 JS，无构建，改完刷新即可）、`test_console.py`（24 例，含真实 HTTP 端点测试）。

## 原唱点播（2026-10-28）

说「放《歌名》」「下一首」「再放一遍」时，bridge 在进 LLM 之前就命中 `songs.pick()`，直接把 `songs/<file>.pcm` 按 16k/mono/s16le 流回板子；不调 LLM、不调 TTS，所以开口几乎无延迟。

- 歌单：`songs/playlist.json`，字段 `title`（显示名）、`aliases`（语音里可能说的别名）、`file`（PCM 文件名）、`seconds`、`source`
- 加歌：把任意音频转成 **16 kHz / mono / s16le** 的 `.pcm` 放进 `songs/`，再在 `playlist.json` 里加一条；`aliases` 写用户可能说的说法。改完重启 bridge（页面「重启」即可）
- 大模型知道歌单：每轮请求前把 `songs.prompt_block()` 追加到 system 消息，列出当前所有曲名，并说明「放的是原唱、不是本人翻唱」。加歌后自动跟着变，不用改提示词
- 音量：走上面的 `/api/volume`，对原唱和 TTS 一视同仁；PCM 内容本身不做重采样，只乘系数

## 屏幕推流（2026-10-28，代码完成、未上机）

设备端一块 2.4 寸 ST7789（横屏 320×240）：上面 320×180 是动画，下面 60 px 两行中文。**中文全部由电脑画**，固件没有字库。屏幕没到货，目前只有测试和编译验证。

开关与可调项（都是环境变量，默认值即关闭）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `SCREEN_ENABLED` | `0` | 总开关。不开时 `build_screen()` 直接返回，bridge 行为与之前完全一致 |
| `SCREEN_FPS` | `10` | 推帧率（实测 10 fps ≈ 70 KB/s、7 KB/帧） |
| `SCREEN_QUALITY` | `75` | 合成 JPEG 的质量 |
| `SCREEN_RECORD_STUCK_S` | `20.0` | 按键后这么久还没等到 `/turn`，屏自动回待机（防止卡在「正在听…」） |
| `SCREEN_FONT` | `C:\Windows\Fonts\msyh.ttc` | 屏上中文的字体文件 |

代码分工：

- `screen_config.py` 全部可调项；`screen_hub.py` 状态机（idle 呼吸 / 12 个随机插播 / 按键反应 60%×1–2 s / listening / thinking / playing / error）+ 帧库 LRU
- `emotion.py` 情绪关键词（2026-10-28 新增）：只读回复文字，长词优先、命中多的赢、同分看谁先出现；映射到 `EMOTION_ANIMS` 里已有的 6 段动画（兴奋/开心/难过/生气/惊讶/害羞），没命中就用「悠闲哼歌」。**不改提示词、不调模型**，换判定只改这一个文件
- `screen_compose.py` 把 320×180 动画和两行中文合成 320×240（实测 3.4 ms/帧）
- `screen_http.py` 一条长连接广播 + `/screen/key`；只有订阅者存在时才合成，没人看就不烧 CPU
- `server.py`：`_stream_turn` 里驱动 `turn_begin/first_audio/turn_end`，**只对 `source == "device"` 生效**，调试台打字不会让板子的屏乱跳；开口那一刻按回复文字挑情绪动画；原唱点播会把「正在播放 · 歌名」写进文字条
- 设备断线/电脑关机时，屏上显示烘焙好的「电脑关机了」静态图（固件内置 24,327 B JPEG），不靠桥画

帧库（**生成物，20 MB，未进 git**，换工作区要重建）：

```
# 需要 WSL 里的 ffmpeg；Windows 侧没有 ffmpeg。源目录是桌宠包 assets/webm
.venv/Scripts/python.exe bridge/tools/build_screen_frames.py
```

- 输出 `bridge/screen_frames/<动画名>/0001.jpg…` + `index.json`（25 个动画，2,449 帧，约 20 MB，10 fps，320×180）
- **色阶必须显式 `in_range=tv:out_range=pc`**：源 webm 是 tv 范围（黑 = Y16）且没有 range 元数据，不写这个黑底会变成 #101010
- 换动画名要同步 `bridge/tools/frame_names.txt` 和 `screen_hub.py` 里的池子常量

测试：`test_screen_frames_build.py`、`test_screen_config.py`、`test_screen_compose.py`、`test_screen_hub.py`、`test_screen_http.py`、`test_screen_server_hooks.py`（后两个起真 HTTP 服务，端口 0）；情绪：`test_emotion.py`、`test_server_emotion.py`；歌单接口：`test_songs.py`、`test_console_songs.py`。

固件侧依赖 **TJpg_Decoder 1.1.0**，本机改过两处（只解内存 JPEG，省 14 KB flash）：

1. `src/User_Config.h` 只留 `#define TJPGD_NO_FFS`，注释掉 `TJPGD_LOAD_SD_LIBRARY`
2. `src/TJpg_Decoder.h` 第 20 行的 `#if (defined (ARDUINO_ARCH_ESP8266) || defined (ESP32))` 末尾加 `&& !defined (TJPGD_NO_FFS)`

装库时 `arduino-cli lib install` 走不了网（本机代理 7890 已死），是直接下 codeload zip 解到 `D:/Arduino15/user/libraries/TJpg_Decoder`。换机器要重做。

## 密钥放在工作区外

`default-workspace` 建有语义索引，密钥落进去会被编码进索引，所以本机约定放用户目录：

```
mkdir ~/.deepseek
echo sk-你的key > ~/.deepseek/key
echo 随便一个共享串 > ~/.deepseek/bridge_token
```

也可用环境变量 `DEEPSEEK_API_KEY` / `BRIDGE_TOKEN`。`bridge_token` 未设置时服务会打印 WARN 并放行——只在专用网络下可接受。

## 大脑路由与本地插件（路由 2026-10-22；规则命令 2026-10-23；本地应急兜底 2026-10-23 已接入，默认关闭）

> 现状：`brains.py` + `llm.py` 已实现 profile 读取、通用 OpenAI 兼容流式客户端、`active`/`fallback`、失败重试、冷却/探测；`server.py` 提供 `--brain` 和 `POST /brain`。**规则命令（stop/time/timer）已在 `pipeline.Turn` 内本地短路**；**本地应急兜底已接入但默认关闭**（`LOCAL_PLUGIN_ENABLED=1` 才启用），云端全挂且本轮未出字时由 2B 模型做简单回复，本地也失败则念固定句。

### 角色边界

- **云端**：人格、情绪、长期记忆、连续对话、哲学思辨。所有会写进人格记忆的内容只走云端。
- **本地**：功能插件，不参与人格。只做语音命令/意图识别、计时提醒、离线小工具、短摘要；不生成陪伴式回复，不写人格记忆，不冒充同一个人。
- **云端不可用或额度耗尽**：明确告知“只能做简单的事”，本地执行功能命令；恢复后回云端。离线期间最多记一条“离线时段”，不灌回人格。

### 云端 profile

所有云端按 OpenAI 兼容接口处理。配置放 `~/.voice-ai/brains.toml`，密钥单独放 `~/.voice-ai/keys/<名字>`，都不进仓库：

```toml
[brain]
active = "deepseek"
fallback = ["kimi", "siliconflow", "local"]

[brain.profiles.deepseek]
base_url = "https://api.deepseek.com"
key_file = "~/.deepseek/key"
model = "deepseek-flash"
max_tokens = 320
timeout_s = 8
first_token_timeout_s = 3
price_in_cny_per_mtok = 2.0
price_out_cny_per_mtok = 8.0
extra_body = { thinking = { type = "disabled" } }

[brain.profiles.kimi]
base_url = "https://api.moonshot.cn/v1"
key_file = "~/.voice-ai/keys/kimi"
model = "以控制台模型名为准"
max_tokens = 320
timeout_s = 8
# price_in_cny_per_mtok = 按控制台账单填
# price_out_cny_per_mtok = 按控制台账单填

[brain.profiles.local]
base_url = "http://127.0.0.1:11434/v1"
key_file = ""
model = "qwen3.5:2b"
max_tokens = 256
timeout_s = 15
price_in_cny_per_mtok = 0.0
price_out_cny_per_mtok = 0.0
```

| 服务 | OpenAI 兼容 base_url |
|---|---|
| DeepSeek | `https://api.deepseek.com` |
| 阿里百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Kimi | `https://api.moonshot.cn/v1` |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` |
| 硅基流动 | `https://api.siliconflow.cn/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| 火山方舟 | `https://ark.cn-beijing.volces.com/api/v3` |

- 模型名以各控制台为准，写进 profile，不硬编码。
- 同一家多个 key 也可以做成多个 profile，用来轮换额度。
- 人格和记忆独立于 provider；换 profile 不改人格、不丢记忆。
- 缺省值：`max_tokens=320`、`timeout_s=30`、`first_token_timeout_s=3.0`；`key_file` 为空表示不带 `Authorization`（本地模型）。
- `brains.toml` 不存在时自动回落到旧的 DeepSeek 单 profile（`~/.deepseek/key`），老部署无需改配置。

### 切换与故障

- 启动：`--brain kimi` 或 `BRAIN_PROFILE=kimi`；启动日志打印当前 brain 状态。
- 运行时：`POST /brain`，body `{"profile":"kimi"}`，带 `X-Auth`，**下一轮生效**；当前回复不打断。返回 `active`/`pinned`/`cooling`。
- 失败链：当前 profile → `fallback` 里的下一个 profile。**本地应急模型不进 profile 链**：只有所有云端 profile 都失败且本轮未出字，`pipeline` 才调用 `local_plugin`；已出字不混脑，本地失败念固定句。
- 首 token 3 s 超时（从发起请求算起，含连接/响应头）、连接失败、5xx/408/425 才重试一次；401/403/402/429 不重试，直接换下一个。
- **已出声不混脑**：一旦向 TTS 送出内容，不在回复中途切换；当前句收尾，下一轮再切。
- 失败后进入冷却，连续失败冷却 5/15/30 分钟；配置里的 primary 由后台线程探测，恢复后自动切回。显式 `/brain` 切换是 pin，不会自动切回 primary。
- `POST /brain` 只接受本机/私网来源（`ipaddress` 判定 loopback/private/link-local），和 `/turn` 一样要 `X-Auth`，不暴露公网。
- 单测：`test_brains.py`（配置/路由/冷却/单价）、`test_brain_stream.py`（假 SSE 服务）、`test_server_brain.py`（`/brain` 与 LAN 判定），共 27 例。

### 用量账本

D 包已实现，写 `~/.voice-ai/usage.jsonl`，每轮一行：

```json
{"ts":"2026-10-22T12:00:00+00:00","profile":"deepseek","prompt_tokens":37,"completion_tokens":23,"cost_cny":0.000258,"fallback_used":false}
```

- 只记录时间、profile、token 数、估算费用、是否 fallback；不记录用户原文、模型回复、密钥、base_url。
- 单价从 profile 的 `price_in_cny_per_mtok` / `price_out_cny_per_mtok` 读取（人民币/百万 token）；不填则 `cost_cny` 为 `null`。
- 默认 deepseek 为高峰价 2 / 8，空闲时段约一半、缓存命中更低，所以账本数字是保守上界，不是实际账单。
- 记录在响应终止块之前完成；写账本失败只打印 WARN，不打断音频。fake LLM 的 e2e 没有 profile，不会写账本。
- 单测：`test_usage.py`（计价/字段/追加）、`test_server_usage.py`（真实 `/turn` 路径与写失败不伤音频），共 12 例。

### 本地插件

| 用途 | 模型 | 量化 | 体积 |
|---|---|---|---|
| 纯意图/命令分类 | Qwen3.5-0.8B | Q4_K_M | 0.51 GiB |
| 默认插件（命令、JSON、短摘要） | Qwen3.5-2B | Q4_K_M | 1.19 GiB |
| 重活、离线长摘要 | Qwen3.5-4B | IQ4_XS | 2.31 GiB |
| 不参与 | 9B | — | 4.8 GiB，与 CosyVoice 放不下 |

- 量化 Q4_K_M 是甜点；Q5 只在显存有余量时用；Q8 没必要。
- **蒸馏是训练手段，不是运行时优化**：插件任务不用 R1-distill，思维链会先吐一长串，慢且费 token；小型 instruct 模型更合适。
- 下载源：ModelScope 首选（`unsloth/Qwen3.5-2B-GGUF` 等仓库体积已核实），Ollama 次选，hf-mirror 兜底；HF 直连在本机不可用。下载前核对仓库 LICENSE，模型与 KV 放 C: NVMe（二进制可留 D:）。
- 本地插件默认无状态、懒加载；每轮结束即释放显存（Ollama `keep_alive=0`）。
- **2026-10-23 已实测并接入**：Ollama **0.40.8** 便携版在 `D:\ollama\`（`ollama.exe` + `lib\`），模型已从 USB 盘搬到 **C: NVMe**，目录 `C:\Users\DELL\.ollama\models`；`qwen3.5:2b-q4_K_M` 1.9 GB（1.9B 参数、Q4_K_M、262144 ctx、Apache-2.0），sha256 校验通过。实测（RTX 3060）：CUDA 后端，25/25 层上 GPU，ctx 4096，显存 **2.36 GB**（卸载后回 33 MiB）；热态首 token **89–233 ms**、生成 **82–117 tok/s**、19-token JSON 约 0.2–0.45 s；C: 上加载 **5.7 s**、首 token 6.5 s（换盘前 D: 冷加载 72 s、重载 24.4 s，已弃用该路径）。启动用 `D:\ollama\start-ollama.cmd`（设 `OLLAMA_MODELS=C:\Users\DELL\.ollama\models` 后 `ollama serve`，监听 `127.0.0.1:11434`，OpenAI 兼容端点为 `/v1`），关窗口即停，或 `taskkill /IM ollama.exe`。
- **关 thinking 的正确写法**：native `/api/chat` 用 `think: false`；**OpenAI 兼容端必须用 `reasoning_effort: "none"`**——实测传 `think:false` 会被忽略，只吐 `delta.reasoning` 且正文为空（64 token 全被思考吃掉），现有 `llm.consume` 只认 `delta.content`，会得到空回复。
- `keep_alive: 0` 实测有效：响应后立即卸载，显存回到基线；`stream_options.include_usage` 可用（实测 33+19=52 token）。
- 该 tag 自带 **331.42M 的 clip 视觉投影**（磁盘含 672 MB），文本插件用不到；官方 tag 去不掉，坚持纯文本需自行用 GGUF 写 Modelfile。

### 应急兜底（已接入 2026-10-23，默认关闭）

- 入口 `bridge/local_plugin.py`：直连 Ollama 的 OpenAI 兼容 `/chat/completions`，只带固定功能 system prompt + 本轮原话，**不带人格、不带记忆、不写记忆**。
- 触发条件只有一个：所有云端 profile 都失败，且本轮**还没有向 TTS 送出任何内容**。已出字不混脑；云端成功但正文为空不算失败，不会切本地。
- 本地也失败（Ollama 未启动/超时/HTTP 错误/空正文）时，念固定一句「云端暂时连不上，我现在只能做简单的事。」，`turn.local_plugin_failed=True`，`turn.error` 保持 `None`。
- 默认 `LOCAL_PLUGIN_ENABLED=0`。打开前先启动 `D:\ollama\start-ollama.cmd`；模型在 C: NVMe，冷加载约 5.7 s，`keep_alive:0` 让每轮结束即卸载。
- 环境变量：`LOCAL_PLUGIN_ENABLED`、`LOCAL_PLUGIN_BASE_URL`（默认 `http://127.0.0.1:11434/v1`）、`LOCAL_PLUGIN_MODEL`（默认 `qwen3.5:2b-q4_K_M`）、`LOCAL_PLUGIN_MAX_TOKENS`（默认 160）、`LOCAL_PLUGIN_TIMEOUT_S`（默认 15）。
- 账本：本地轮次记 `profile=local`、`fallback_used=true`；`brains.toml` 没有 `local` profile 时 `cost_cny=null`。固定句没有 usage，不写账本。
- 实测（2026-10-23，RTX 3060，C: NVMe）：我方真实请求体冷加载 **7.53 s**、回复 41 字/24 chunk/25 completion token，`reasoning_chars=0`；结束后 `ollama ps` 为空、显存回 33 MiB，确认 `reasoning_effort:"none"` 与 `keep_alive:0` 在 OpenAI 端点都生效。
- 单测：`test_local_plugin.py`（6 例）、`test_pipeline.py`（10 例，含 4 个兜底场景）、`test_memory_integration` 的本地插件跳过记忆写入 1 例。

### 规则命令（C，已接入 2026-10-23）

- 入口：`commands.parse_command(text) -> Command | None`，`kind` 只有 `stop` / `time` / `timer`；`local_commands.reply_for(cmd)` 只生成回复文本。
- `pipeline.Turn._handle_local_command()` 在组装消息、调用 LLM 之前短路；命中后文本仍走原有切句、TTS 与 chunked PCM，`turn.local_command` 记录 kind。
- 本地命令不读记忆、不调 LLM；`server.enqueue_memory` 对 `local_command` 非空回合跳过 writer，不写记忆、不进 `recent_cache`、不写用量账本。
- 回复：`stop` →「好。」；`time` → 本机时间；`timer` → 确认时长/标签，并明确「现在还不能到点主动出声」。
- **不做定时线程**：HTTP 是请求/响应契约，回合外无法主动出声；真正到点提醒需要设备端长轮询/WebSocket 或本地蜂鸣器。
- 否定误判已修：`五分钟后别叫我` 不再解析成 timer。单测 `test_commands.py`（54 例）+ `test_local_commands.py`（13 例）。

### 上下文与长期记忆

“不带 DSH 上下文和工具”只表示不把 DSH 的系统提示、工作区文件、工具定义、完整会话历史塞进语音请求；**不等于没有长期记忆**。

每轮实际发送：

1. 人格设定（≤120 token）
2. 检索到的 top-3 长期记忆（每条 ≤30 字）
3. 最近 2 轮对话 + 100 字摘要
4. 本轮用户输入

长期记忆存外部，按需检索；**上下文窗口 ≠ 长期记忆**。本地插件只拿当前命令，不写人格记忆；DSH 的记忆不自动合并，需要时显式导入。

### 五层记忆（2026-10-23：阶段 1–5 完成；真实目录已启用）

- 代码：`memory_store.py` / `memory_score.py` / `memory_extract.py` / `memory_context.py` / `memory_maintenance.py` / `memory_admin.py` / `memory_writer.py`；读路径入口 `memory_context.build_for_turn()`，写路径入口 `server.enqueue_memory()`。
- 开关：`MEMORY_ENABLED=1` 才启用；代码默认 `0`（为了不污染测试与实跑脚本）。**日常启动用项目根的 `start-bridge.cmd`，它设 `MEMORY_ENABLED=1`、`MEMORY_DRY_RUN=0` 后再启动 `server.py`**；关闭时 `pipeline`/`server` 行为与旧代码一致（两条消息、两参数 `begin`、不构造 writer）。启动时会打印一行 `memory: disabled` 或 `memory: enabled dir=... dry_run=...`，据此确认本次是否真实写入。
- 写入：代码默认 `MEMORY_DRY_RUN=1`（dry-run 只做敏感预检、抽取、会话预览与计数，不写 facts/episodes/sessions、不调 `mark_used`、不跑维护）。**真实目录已于 2026-10-23 由 `start-bridge.cmd` 以 `MEMORY_DRY_RUN=0` 启用**：首轮生成 `sessions/*.json`、`state.json`，第二轮读到上一轮摘要（`L2=26`、`L4=1`）；`store.read_only` 时仍强制 dry-run。
- 维护（阶段 4）：真实写入模式下启动评估一次，之后每个成功写盘回合把“距上次维护新增回合数”+1 并调用 `MemoryMaintenance.maybe_run`；24 h 或 50 回合触发晋升/降级/容量清理，维护异常只计数。dry-run 完全不调用维护。
- 退出补刷：`serve()` 在 `finally` 调 `writer.stop()` + `server_close()`；`run_local`/`record_mic` 入队后停止 writer。
- debug：`MEMORY_DEBUG=1` 时读路径打印 `memory: L1=... L2=... L3=... L4=... total=x/budget trimmed=... current=... warnings=...`，只含计数；`memory_debug.jsonl` 未实现。
- 数据：`~/.voice-ai/memory/`（工作区外，已于 2026-10-23 创建）；dry-run 不创建目录；真实写入才落 facts/episodes/sessions。事实/情节按规则抽取（「记住…」「我叫…」「我喜欢…」等句式），普通陈述句只进会话摘要。
- 预算：`MEMORY_INPUT_BUDGET_TOKENS=1050` 硬上限；本轮输入 `MEMORY_CURRENT_INPUT_MAX_CHARS=500`；超限按 摘要→记忆→历史 裁剪，人格与本轮输入不砍。
- 会话：`memory_store.parse_session_id()` 独立解析 `session_id`；响应头 `X-Session-Id` 仍是旧值 `d=dev1`，记忆桶不依赖该头。
- 读路径投影与缓存上限（阶段 5）：`active_facts()`/`active_episodes()` 只复制顶层字段并过滤 active，返回值只读，调用方不得修改嵌套 `history`；`facts()`/`episodes()` 仍深拷贝供维护/管理。`MemoryStore(max_sessions=64)` 与 `RecentCache(max_sessions=64)` 做 LRU，`MemoryWriter(seen_gen_limit=8192)` 做 FIFO 去重上限。
- 测试：9 个 `test_memory_*.py` 共 **204 例**（151 纯模块 + 27 writer + 26 集成），只用标准库、不联网、不占 GPU。覆盖 dry-run 零落盘、敏感回合零缓存、终块前 `recent_cache` 可见、命中每 5 回合批量刷、维护启动/50 回合触发、debug 不含原文、退出补刷、`schema_version` 过新只读告警与非法数值环境变量回退、只读投影与缓存界限、本地命令与本地插件回合跳过 LLM 与记忆、启动时打印 `memory: enabled/disabled` 状态行。
- 回归入口：用显式清单（18 个 unittest 模块、**379 例** + 3 个脚本自检 **27 例**）；**不要用裸 `unittest discover`**——`test_e2e.py` / `test_live.py` / `test_edge_content.py` 是脚本式测试，import 即执行真实 HTTP/ASR/TTS/云端调用（`test_live` 启动 CosyVoice、占 GPU、写用量账本）。
- 读路径延迟：阶段 5 前 500 episodes + 500 字输入 + 预热 token cache 的 `build_for_turn` 为 p50 6.1–13.0 ms / p95 6.6–15.0 ms；只读投影后本机复测 **p50 1.93 ms / p95 2.06 ms**（`active_episodes` p50 0.10 ms，原 3.90 ms）。**硬门 p95 ≤20 ms**，<5 ms 目标已达成。
- 未做：`memory_debug.jsonl`、`MEMORY_LLM`/`memory_usage.jsonl`、p95 自动回归门。**未用 `start-bridge.cmd` 而直接起服务时仍是 dry-run，`hits` 不增长、晋升不会发生。**

### Token 预算

常态目标约 480 输入 + 50 输出 ≈ 530 token/轮；五层记忆启用后**硬上限 1050 token/轮**（L0–L3 + 本轮 500 字 + 最近两轮），按 `deepseek-flash` 空闲价约 ¥0.0002/轮。账本默认按高峰价估算，会更高。功能命令走本地，云端额度留给“有人格”的对话；DSH agent 的上下文和工具不进入语音链路。

### 落地顺序

1. ~~`brains.toml` + profile 读取 + 通用 OpenAI 客户端~~ → 已完成（`brains.py` / `llm.py`）。
2. ~~`/brain` + fallback 链 + 冷却/探测~~ → 已完成（`server.py` / `brains.py`）。
3. ~~用量账本~~ → 已完成（`usage.py`，`~/.voice-ai/usage.jsonl`）。
4. ~~五层记忆阶段 1（纯模块）~~ → 已完成（148 例）。
5. ~~五层记忆阶段 2（只读接入，默认关闭）~~ → 已完成（集成 16 例）。
6. ~~五层记忆阶段 3（异步写队列 + dry-run）~~ → 已完成（2026-10-23，writer 18 例、集成 19 例）；默认不落盘。
7. ~~五层记忆阶段 4（维护 + 可观测性 + 退出补刷）~~ → 已完成（2026-10-23，writer 25 例、集成 22 例）；**真实目录同日晚已启用**（`start-bridge.cmd`，两轮写入验证）。
8. ~~五层记忆阶段 5（读路径只读投影 + 缓存上限）~~ → 已完成（2026-10-23）；记忆 204 例。
9. ~~规则命令（C）接入~~ → 已完成（2026-10-23，`commands.py` + `local_commands.py`）；`timer` 只确认、不到点响。
10. ~~下载 Qwen3.5-2B Q4_K_M 并接入本地插件入口~~ → 已接入应急兜底（2026-10-23，`local_plugin.py`，默认关闭）；只做简单问答，不承载人格与记忆。
11. 最后再考虑语音口令切换和故障模式的设备提示。

## 实测（2026-10-27，长回复断续：真机定位 + 定案）

修前真机 55.7 秒回复在 3 秒缓冲下 46 次断流（≈1.47 秒，2.6%），缺口集中在开头。逐句遥测（`TTS_TRACE=1`）把根因钉在一个公式上：

**开局缺口 ≈ max(0, 句间停顿 + 合成(第二句) − 音频(第一句))**

首句是合成完才发的**整段突发**，播放一开始手里就握着整句音频，所以起播水位不改变这个差值。真机单句经济学：音频 ≈ **155–170 ms/字**，合成 ≈ **110 ms/字 + 0.6 s**——只有 18–21 字的句子刚好不亏，22 字以上每句净赚 0.4–2 秒（中段不卡就是靠这个，句间 0.6–1.8 秒的 LLM 停顿由 3 秒缓冲吸收）。

| 轮次 | 首句→第二句 | 首句音频 | 第二句合成 | 预测 | 实测断流 |
| --- | --- | --- | --- | --- | --- |
| 09:47（旧规则） | ~22 → ? | 3.4 s | ~4.2 s | −0.8 | 45 次 |
| ≥18 首句 | 29 → 40 | 4.54 s | 4.26 s | +0.28 | 0 |
| ≥18 首句 | 26 → 38 | 4.44 s | 4.36 s | +0.08 | 0 |
| ≥18 首句 | 31 → 37 | 4.10 s | 4.78 s | −0.68 | 23 次 |
| **新规则** | 24 → 30 | 3.96 s | 4.24 s | −0.28 | **5 次** |

- **已排除**：加深缓冲（6 秒仍 57 次）、fp16 autocast（RTF 0.91–0.93）、流式合成（首块 2.84–3.60 秒）、混合切句（97–121 次）、**抬起始水位**（`PLAY_PREBUFFER_BYTES` 1.8 s 仍掉 23 次，已回退到 160 ms）。
- **定案**：首句 ≥18 字、**第二句 ≥20 字且在逗号处切、26 字硬上限**、第三句起 ≥18 字。`config.SENTENCE_MIN_CHARS` / `SENTENCE_FIRST_MIN_CHARS` / `SENTENCE_SECOND_MIN_CHARS` / `SENTENCE_SECOND_MAX_CHARS`，`Turn` 全部透传（`SentenceSplitter` 类默认值仍是 8/5/None，不影响它自己的测试）。强切只影响第二句且只在 26 字内没标点时发生，代价是句中多一个停顿（用户已接受）。
- 真机：69.1 秒回复 **0 次断流**（补 26 字上限后）；72.5 秒回复 5 次（0.16 秒，上限之前）；62.8 / 78.1 / 81.4 / 22.8 / 7.2 / 5.0 / 2.9 秒七轮 0 次。首个 PCM 到达 **4.4–5.2 秒**（≥18 首句的直接代价）。
- 残余风险：只在「第二句 26 字内无标点」的轮次出现强切停顿，尚无一例触发。
- 完整实验记录见 `NEXT-AI-2026-10-27-tts-latency.md` §3.9–§3.15。早期基于模拟器的「1.9 秒固定开销」拟合被真机推翻（真机固定开销约 0.6 秒）。

## 实测（2026-10-20，edge-tts 路径 + 假 LLM）

一轮真实往返的分环节计时：

| 环节 | 首轮 | 次轮（热） |
|---|---|---|
| ASR（SenseVoice，**CPU**） | 334 ms | 233 ms |
| 收满首句（假 LLM，TTFT 0.03 s） | 456 ms | 355 ms |
| 首句合成完 = **首字节送达** | **1993 ms** | **1718 ms** |
| edge worker 合成 13 字 | 1491 ms | 1312 ms |
| 打断后闭流耗时 | — | 1.32 s（`/stop` 生效） |

SenseVoice 在 CPU 上单次调用 **0.15–0.40 s**（`rtf_avg` 0.04–0.25，随音频长度变；模型加载 9–15 s）。所以 ASR 完全不碰 GPU，把 6 GB 显存整块留给 CosyVoice。

## 实测（2026-10-20，CosyVoice 主线，`cosy_probe.py`）

单句合成耗时（同一组句子跑三轮，第三轮加了加载期预热）：

| 句子 | 无预热 | 有预热 | 音频 |
|---|---|---|---|
| 7 字 | 3431 / 2694 ms | **1705 ms** | 1.14 s |
| 9 字 | 1533 ms | 1767 ms | 1.34 s |
| 16 字 | 1754 / 2317 ms | 2763 ms | 2.52 s |
| 23 字 | 2740 / 2883 ms | 3070 ms | 3.27 s |

- **稳态 ≈125–206 ms/字、RTF 0.84–1.5**。切句之后 CosyVoice 的一句话是 1.4–3.1 s，不是整段回复外推出来的 5.7 s。
- **首调惩罚 ~1.2 s**（同一句 2694 → 1441 ms），已用加载期先合成「好。」消除；`COSYVOICE_TIMEOUT` 相应从 6.0 s 提到 **9.0 s**（40 字上限的一句要 5–6 s，超时会在回复中途换嗓子，比慢更糟）。
- 显存 **≈2.0 GB**（GPU 总占用 2158 MB，基线 142 MB，停止后回到 142 MB）。加载 15–20 s；先前那个 53.9 s 是**冷页缓存**，不是常态。
- **内容回读全对**：五条合成音频经 SenseVoice 听回来，逐字与发出的文本一致（逗号也在），说话人「中文女」正常解析。`cosy_verify.py` / `test_edge_content.py` 就是干这个的。

## 实测（2026-10-20 晚，真实 DeepSeek + CosyVoice 全链，`test_live.py`）

`~/.deepseek/key` 与 `~/.deepseek/bridge_token` 均已配置（无 token 实测被 401 拒绝）。真 ASR(CPU) → deepseek-flash 流式 → 切句 → CosyVoice → chunked PCM，两轮实测：

| 环节 | turn 1 | turn 2 |
|---|---|---|
| ASR（SenseVoice，CPU） | 397 ms | 497 ms |
| 真实 TTFT（deepseek-flash） | **0.22 s** | 0.52 s |
| 收满首句 | 870 ms | 1382 ms |
| 首句合成完 = **首字节送达** | **2394 ms** | 8859 ms |
| 首句（字数 × 耗时） | 7 字 1476 ms（210 ms/字） | 16 字 7339 ms（459 ms/字） |
| `/stop` 后闭流 | — | 7.61 s |

- **真实 TTFT 0.1–0.5 s（四轮采样 0.109/0.220/0.226/0.517），不是外推的 1.0–1.5 s**。云端这一跳被证伪为瓶颈。`thinking:disabled` 生效：usage 无 reasoning tokens，completion 23–30/轮。
- **首字节 ≈ 首句字数 × ms/字，两个乘数都咬人**。persona 的「好的，」开场曾让首句只有 6–8 字（turn 1 = 2.4 s，达标边缘）；LLM 也会跳过开场白直接 16 字长句开局（turn 2 = 8.9 s）。连跑后 ms/字从 ~210 涨到 ~450——探针单测三轮都稳在 125–206，当晚用 `throttle_probe.py` 做了三阶段归因，见下节。**2026-10-27 起切句改为所有句子 ≥18 字（含首句），本节的短首句配置已不再使用，见上节。**
- **`/stop` 闭流要等 worker 里在合成中的那句跑完**（实测 7.61 s，上界 = `COSYVOICE_TIMEOUT` 9 s）：进行中的推理无法取消。设备按契约在 barge-in 时本地 flush，所以这不是可听延迟，只是连接多挂几秒。LLM 侧不需要打断——它 0.5–0.8 s 就说完了，人嘴跟不上（`llm_aborted=False` 是常态；LLM 中断路径由单测覆盖）。
- **内容回读相似度 0.92 / 0.85**，句子级逐字吻合；「开饭→爱饭」「下午5点→下5点」是数字与近音的 ASR 误听，探针已证 TTS 内容无损。回复音频在 `out/live_reply1.wav`，可直接听。
- 加载预算：页缓存热 CosyVoice 22 s / SenseVoice 13 s；冷缓存（隔几小时后首启）55 s / 32 s。三进程同驻时空闲内存一度只剩 2.1 GB，跑服务前关掉大内存应用。

## 实测（2026-10-21 01:34 复测，`throttle_probe.py`，ms/字 归因修正）

同一常驻 worker、无 HTTP 层，三阶段对照 + 1 Hz GPU 遥测（原始数据 `voice-ai/test-data/throttle_probe/throttle_probe.json`；该文件随后被 09:01 的重启后复测覆盖，本节的数字保留在本文中）：

| 阶段 | 每轮均值 ms/字 |
|---|---|
| A 纯 GPU | 178.6 / 213.2 / **396.5** / 172.1 |
| B SenseVoice 常驻 | 183.3 / 176.5 / 145.0 |
| C 降温后 | 163.5 |

- **GPU 争用是一种已复现的慢因，但不是唯一**：慢轮 A r2（396.5 ms/字）与另一会话的真人语音一轮（CosyVoice 合成，01:36 前后，同一张卡）重叠；该轮 SM 时钟反而升到 1702 MHz、功耗 43 W——两个 300M 模型同时在卡上。其余 7 轮全是 145–213 ms/字。**但 01:43 常驻服务 turn 1 在卡上无任何其他计算进程时也慢到 364–558 ms/字（见下节），所以"慢＝争用"不能解释全部。**
- **热节流排除**：温度 72–76 °C，无 SW_POWER_CAP / HW_THERMAL。
- **"低时钟"解释不成立**：快轮平均 SM 时钟 788 MHz，慢轮 974 MHz——快轮同样跑在 780 MHz。
- **内存压力排除**：B 阶段空闲 RAM 1.95–3.13 GB，速度无差异。
- **bridge 层排除**：探针直接驱动 worker。
- **01:27（308–483 ms/字）和 23:57（310–362）两次慢例**没有慢轮期间的 GPU 进程快照。`throttle_probe.py` 现已带 1 Hz `pid,process_name` 采样；2026-10-21 09:01 重启后复测（见下节）全程无第三方 CUDA 进程且 8 轮全快——慢例归为长时间运行后的机器状态，不是夜间、温度或内存。
- 规划按**快态 125–215 ms/字**；真遇慢态先重启恢复，复测前确认没有别的会话在跑 GPU。

## 实测（2026-10-21 01:43–01:45，常驻服务，CosyVoice 全链干净样本）

`server.py --serve` 常驻（SenseVoice + edge + CosyVoice 全就绪），同一段 16k 录音连跑三轮。跑前/跑后 `nvidia-smi` 只看到本服务的 py311 worker 与桌面进程；慢轮窗口内没有 pid 级采样：

| 轮 | ASR | 真实 TTFT | 首句字数 | 首句合成 | **首字节** | ms/字（首句 / 后续句） |
|---|---|---|---|---|---|---|
| turn 1（服务冷启动后首轮） | 682 ms | 1.214 s | 8 | 3034 ms | **5341 ms** | 364 / 558 |
| turn 2 | 281 ms | 0.639 s | 18 | 2102 ms | **3539 ms** | 117 / 172 |
| turn 3 | 347 ms | 0.567 s | 7 | 1696 ms | **2895 ms** | 242 / 167–169 |

- **首字节 ≈ ASR + TTFT + 首句字数 × ms/字**，三项都实测到了。turn 3 最短（7 字首句）**2895 ms**；turn 2 首句 18 字却只 3539 ms，因为合成在快状态。
- **快状态 117–172 ms/字**：turn 2/3 后续句 SM 1837–1942 MHz、75–94 W、最高 86 °C。turn 1 落在慢状态（364–558 ms/字）；事后查进程表只剩本 worker，慢轮期间没有 pid 级采样。**09:01 重启后带 pid 采样复测 8 轮全快、全程无第三方 CUDA 进程（见下节）——慢态与机器长时间运行相关，重启即恢复。**
- **首句合成含 DVFS 爬升**：GPU 空闲后第一句从 210 MHz 起步，约 2 s 才到满频；turn 3 的 7 字首句为此花了 242 ms/字（后续句 167–169）。
- turn 1 的 TTFT 1.214 s 是新进程首调（TLS/连接冷），turn 2/3 降到 0.64/0.57 s，与 `test_live` 的 0.1–0.5 s 同量级。
- 回复音频：`out/clean_turn.wav` / `clean_turn2.wav` / `clean_turn3.wav`（3.65 / 3.81 / 8.53 s）。
- 距 1.5–2 s 目标：7 字首句 + 快状态仍要 2.9 s；再压只能减首句字数（≤6 字 + 120 ms/字 ≈ 2.2 s）、消掉爬频，或首句走 edge 兜底。

## 实测（2026-10-21 08:55–09:01，重启后复测，慢态未复现）

冷启动后重跑同一探针，首次带 1 Hz `pid,process_name` 采样（原始数据覆盖到同一路径）：

| 阶段 | 每轮均值 ms/字 |
|---|---|
| A 纯 GPU | 140.5 / 132.0 / 132.2 / 130.5 |
| B SenseVoice 常驻（空闲 RAM 降到 0.96 GB） | 129.5 / 128.2 / 126.6 |
| C 降温后（实际 58 °C，240 s 未降到基线 53 °C） | 128.7 |

- **8 轮全部 126.6–140.5 ms/字，慢态（308–558）未复现。** 全程 pid 采样里只有本探针的 py311 worker，没有第三方 CUDA 进程。
- **内存 / 热 / 争用三项排除**：SenseVoice 常驻、空闲内存只剩 0.96 GB 不降速；温度 51→81 °C 不降速；偶发 `HW_SLOWDOWN+HW_THERMAL` 位时 SM 仍 1950–1987 MHz、速度不变。
- **与 01:43 turn 1 慢态的唯一差别是重启**：慢态因此归为长时间运行后积累的机器状态（具体项未定位），不是热、内存、bridge 或可观测的并发进程。
- **DVFS 爬升仍在**：每个「GPU 空闲后第一句」固定多付一笔（7 字句 ~1.0–1.3 s，采样窗口均值 210 MHz），后续句立刻回到 ~100–130 ms/字。
- 结论：规划按**快态 125–215 ms/字**；7–8 字首句的整链首字节约 **2.1–2.4 s**（ASR ~0.3 + TTFT ~0.6 + 首句 ~1.2）。遇慢态用重启恢复，而不是换引擎。**2026-10-27 起首句固定 ≥18 字，整链首字节预期 3.5–4.2 s（模拟 3.56–3.73 s），这是为消掉长回复开头空档付的价。**

## 两处要纠正的估算

1. **edge-tts 每句多付约 1.0 s 固定开销**（2 字 RTF 1.14、17 字 RTF 0.43）。它不是纯 RTF 0.21 的线性模型：`RTF×时长 + 1.0 s`。原因是 7.3.1 每条句子新开一条 WebSocket，且 `ClientSession(connector=...)` 未设 `connector_owner=False`，共享连接池这条路走不通（已在源码核实）。后续句子的这笔开销能被流水线隐藏，首句隐藏不了。
2. **首句出声**（已在真实链路实测，见上节）：TTFT 实测 0.1–0.5 s（不是粗测的 1.0–1.5 s），首字节实测 2.4 s（7 字首句）到 8.9 s（16 字首句 + 连跑变慢）。**首句字数是第一杠杆，TTFT 不是瓶颈**。edge 的真实优势是不占显存，不是快得多。8.9 s 那一轮不是常态——常驻服务下同样的首句长度是 3.5 s（见 01:43 节）。

距 1.5–2 s 目标的差距不在云端，在首句字数 × 合成速度：persona 已把多数回合首句压到 6–8 字，剩余手段是压 ms/字（快态已到 100–130，慢态靠重启恢复）和更激进的首句切分。**2026-10-27 定案与此相反：为了长回复开头不断流，首句反而加长到 ≥18 字，1.5–2 s 降为长期目标（见上节）。**

## 待办

- ~~CosyVoice worker 尚未实测~~ → 已实测，见上表（跑前与会话 `75cb6df7` 让卡）
- ~~真实 DeepSeek 调用未验证~~ → 已验证（key 与 bridge_token 已配置，见 `test_live.py` 实测节）
- ~~连跑后 ms/字 210→450 待归因~~ → 复测（2026-10-21 01:34）：干净轮 145–213 ms/字，唯一的 396 慢轮与另一会话的 GPU 占用重叠——**争用可复现**，热节流/低时钟/内存均不成立；01:27、23:57 与 01:43 turn 1 的慢例在 09:01 重启后带 pid 采样复测中未再出现（8 轮 126.6–140.5），**慢态归为长时间运行后的机器状态，重启即恢复**；规划按快态 125–215 ms/字
- 设备侧起始水位 150–200 ms 未实现（无硬件）
- ~~尚未用真人语音跑过完整一轮~~ → 已跑（2026-10-21 01:36，`--mic --play`）：用户说「好，说话了。」→ ASR 297 ms → TTFT 0.309 s → 首句 11 字 CosyVoice 2967 ms（270 ms/字，**上界**：与另一会话 GPU 占用重叠）→ 首字节 **3967 ms**，退出码 0，扬声器播放正常。
- ~~大脑路由（profile + `/brain` + fallback）~~ → 已实现（2026-10-22，见「大脑路由与本地插件」节）
- ~~五层记忆阶段 1/2/3/4/5（纯模块 + 只读接入 + writer dry-run + 维护/退出补刷 + 读路径投影/缓存上限）~~ → 已完成（2026-10-23，204 例）；真实目录已启用，日常用 `start-bridge.cmd` 启动
- ~~规则命令（C）~~ → 已接入（2026-10-23，54 + 13 例）；`timer` 只确认、不到点响
- ~~五层记忆真实目录启用（备份 + `MEMORY_DRY_RUN=0` + 用户确认）~~ → 已完成（2026-10-23；原目录不存在，无需备份；两轮真实写入验证，`start-bridge.cmd` 为日常入口）
- 硬件上机（RECORD_ONLY → TURN → TURN_STOP）—— 固件三模式编译通过，未烧录；按 `handoff/G-hardware-bringup.md`
- ~~本地功能插件~~ → 应急兜底已接入（2026-10-23，`local_plugin.py`，默认关闭）；只做简单问答、不写记忆，固定句兜底；模型在 C: NVMe，Ollama 需手动启动

`--mic` 已可用：`sounddevice` 0.5.6 装在 `.venv`，默认输入设备实测以 **44100 Hz / 4 ch** 原生运行，录音后重采样到 16k 再送 ASR（不能直接按 16k 打开阵列设备）。
