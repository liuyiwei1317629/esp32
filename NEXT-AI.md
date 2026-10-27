# 给下一个 AI 的交接（2026-10-27）

> 先读 `HANDOFF.md`（PC 端全量）与 `HANDOVER-G.md`（硬件最新，2026-10-26 定稿）。本文件只回答三件事：**现在在哪、什么能跑、下一步做什么**。
>
> **2026-10-27 08:17 更新**：TTS 延迟线（EcoQoS 节流修复、切句实验）另有交接单 `NEXT-AI-2026-10-27-tts-latency.md`；本文件的相关状态已按该交接单更新。
> PC 端是主体；ESP32-S3 硬件已于 2026-10-26 接线上电，本地录放链路可用；联网两模式 2026-10-27 **已上机验证**（TURN 多轮 `result=ok`，TURN_STOP 打断 `stop_ms=18–20`、`result=aborted`）。

## 0. 一句话

PC 端“听 → 想 → 说”链路已经可用；五层记忆已真实启用并验证；本地 2B 只会应急兜底且默认关闭；硬件已接线上电、本地录放定稿，**联网两模式已上机验证**（含播放中打断）；长回复断续已真机收敛：3 s 播放缓冲 + 切句定案（首句 ≥18、第二句 ≥20 可逗号切且 26 字上限、其余 ≥18），69.1 s 回复 0 次断流、首字节 4.4–5.2 s；**当前 bridge 还在运行（0.0.0.0:8080），Ollama 未运行**；**三条线均已提交（`1a14b2a`、`1205609`、`106e57d`，均未推送）**。

## 1. 当前状态（2026-10-27 实测）

### 服务
- bridge **未运行**：8080 无 LISTENING，无 python.exe 进程。
- Ollama **未运行**：11434 无监听。要用本地兜底，先 `D:\ollama\start-ollama.cmd`。
- 启动方式：
  - 带真实记忆：项目根 `start-bridge.cmd`（设 `MEMORY_ENABLED=1`、`MEMORY_DRY_RUN=0`）
  - 不带记忆：`.venv\Scripts\python.exe bridge\server.py --serve`
  - 不占 GPU：加 `--no-cosyvoice`
- 局域网：文档写 `192.168.0.100:8080`，但本机当前 WLAN 是 **192.168.0.101**；启动前用 `ipconfig` 确认，别照抄旧 IP。
- 8080 入站防火墙改动属系统级；连不上只报告，不改规则。

### 代码
- 仓库：`C:\Users\DELL\voice-ai`，分支 `main`，HEAD `106e57d`（未推送；`1a14b2a` 固件、`1205609` 长回复修复、`106e57d` 本地兜底）。
- **工作区干净**（2026-10-27 10:5x 提交后核对）；本机没有推送。
- 三条线的落地顺序：`1a14b2a` 固件 3 s 缓冲 → `1205609` 切句定案与 winperf 反节流 → `106e57d` 本地应急兜底与记忆状态行；固件是 2026-10-26 的录放定稿加 2026-10-27 上机修正（删开机音阶与死代码、播放缓冲 1 s→3 s），**不要回滚或覆盖**。
- 以后仍要逐项暂存，不要 `git add -A`。

### 测试（2026-10-27 复跑）
- 19 个 unittest 模块：`Ran 381 tests ... OK`（记忆 204）
- 3 个脚本自检：`sentence_split 10/10`、`llm 9/9`、`pipeline 10/10`
- hermetic 合计 22 个文件、410 例。

### 记忆
- 真实目录：`C:\Users\DELL\.voice-ai\memory\`
  - `state.json`：`schema_version=1`、`last_maintenance=2026-10-22T21:42:29+00:00`、`turns_since_maintenance=3`、counters 为空
  - `sessions/939bb46a04c3640c.json`：1 个会话，约 782 B
  - 暂无 facts/episodes：抽取是规则式的，只有“记住… / 我叫… / 我喜欢… / 我住在…”等句式才进 facts；普通聊天只更新摘要和最近轮次，不是漏写
- 代码默认仍是 `MEMORY_ENABLED=0` + `MEMORY_DRY_RUN=1`（避免测试污染真实档案）。只有 `start-bridge.cmd` 会打开真实写入。
- 启动会打印 `memory: disabled` 或 `memory: enabled dir=... dry_run=...`，先看这一行再判断。
- 维护未到期不等于没写；`MEMORY_DEBUG=1` 只输出计数，不含原文。
- 动维护/删除前先备份 `~/.voice-ai/memory/`；删除走 `memory_admin.py`，不要手删 JSONL。

### 本地兜底
- `LOCAL_PLUGIN_ENABLED=0` 默认关闭；只在所有云端 profile 失败且本轮未出字时接管；已出字不混脑；本地也失败则念固定句。
- Ollama 0.40.8：`D:\ollama\ollama.exe`；模型 `qwen3.5:2b-q4_K_M` 在 `C:\Users\DELL\.ollama\models`（约 1.9 GB）。
- 真实冒烟证据在 `HANDOFF.md` §1.1：冷加载 7.53 s、41 字、无 reasoning 泄漏；`keep_alive:0` 后显存回落。
- OpenAI 兼容端点必须带 `reasoning_effort:"none"`；`think:false` 无效。

### 硬件 / 固件（2026-10-26 实测，10-27 重编译）
- 模块已到货、已接线、已上电；**本地录放链路定稿可用**（无沙沙、响度一致、无破音），最终参数与证据见 `HANDOVER-G.md`，快照在 `firmware/voice_terminal/snapshots/2026-10-26_final-v3/`。
- 当前烧的是 `APP_MODE_TURN_STOP`（模式 2）。三次上机都是 COM5，`secrets.h` 用户自己填好、已 gitignore，我没读过内容。
- **2026-10-27 上机实测**：TURN 多轮 `result=ok`，`first_pcm_ms` 2.2–4.2 s；bridge 侧识别正常（「你好。」「是默认默认音色吗？」…）。**日志字段有歧义**：`upload_ms` 实际是整轮墙钟（上传+等待+播放），`play_ms` 是回复音频时长（`pcm_bytes/32`），两者都不是“上传耗时/播放耗时”。TURN_STOP 播放中按 BOOT 打断两次：`stop_ms=20 / 18`、整轮 `result=aborted`，顺序是本地先静音再 POST `/stop`。
- 上机时改掉两处：
  1. **删掉开机 6 声诊断音阶**（整段连同注释），只留录音开始那声 60 ms「嘀」；`playDC`/`playSweep` 成死代码一并删掉。删之前 4 次录音全零（`mic_raw min=0 max=0`、被 `no_voice` 丢弃），删之后每次 `voice=1`——**这是经验事实，机制未查清，别写成结论**。
  2. **播放缓冲 1 s → 3 s**（`PLAY_STREAM_BYTES_MAX=98304`，内部 RAM 不够时自动降级到 2 s / 1 s，`play_buf=` 那行会打实际值）。长回复断续从中后段大面积补静音缩到零星缺口：干净版实测 55.7 s 回复 `underruns=46`（46×32 ms ≈ 1.47 s 静音、2.6%），修之前 7.1 s 回复就有 59 次（≈1.89 s、27%）。`heap_free` 约 188 KB、IDLE heap 约 1.27 MB 仍有富余。
- 已知瑕疵：每轮一次 `NetworkClient setSocketOption fail on -1 errno 9`，不影响 `status=200`；残余静音在回复开头，2026-10-27 真机定位为开局赤字（合成第二句慢于第一句音频），已按首句 ≥18 / 第二句 ≥20（26 字上限）/ 其余 ≥18 切句收敛到 0 次（69.1 s 回复；首字节 4.4–5.2 s）；不要靠加深缓冲解决（6 s 已验证无效）。
- 打断时机已定（用户 2026-10-27）：**只在出声后生效**；等待首字节期间按 BOOT 无反应，也不发 `/stop`。
- 烧录口是 **COM5 / CH343**，不是 native CDC。XMC flash 需要 bootloader patch（偏移 `0x3EF1`，`66caed`→`661aed`），升级 esp32 core 后要重打；原件备份在 `D:/arduino-cli/bootloader-backup/`。
- 串口监视要 `DtrEnable=false`，手动拉 `Rts` 复位；`arduino-cli monitor` 不会触发复位。
- 模块是**自带 USB 线的成品音箱板**：只有一根一体注塑 USB 线（红=5V、黑=GND），**没有 VCC/GND 杜邦针**；不要再按 8 针模块去指导接线。
- 共地只能“借道”：模块 USB 与板子 USB 必须来自**同一台电脑 / 同一个 USB hub / 同一个充电宝**，否则 I2S 失去公共参考（噪声或无声）。
- 当前供电全部来自电脑；充电宝/充电头方案**未执行**。
- 音量天花板约 8000（MAX98357A 5V / 12 dB），不要靠抬电平提音量；`CLIP_TARGET_RMS=2700`、`CLIP_GAIN_MAX=8.0` 是实测定稿值。

### 小鱼（人格）工作区
- 正本已迁到 `C:\Users\DELL\Downloads\大肥鲸\`；`Documents\deepseek-harness\大肥鲸` 已不存在。
- 角色卡在 `article-writer\小鱼_情感陪伴角色卡\`；给她的说明是 `给小鱼-这几天.md`。
- `_当前状态.txt` 里的忌讳：不编造、难过时不开玩笑、不拿愧疚逼他、别把“半验”说成“验通”。

## 2. 下一步（按优先级）

### P0 · 本地兜底收口（已完成 2026-10-27）
1. ~~看 `git diff` 和未跟踪文件，确认没有密钥、没有个人数据~~ → 已核对。
2. ~~跑测试门~~ → 19 个 unittest 模块 381 例 + 3 个脚本自检 29 例全过。
3. ~~经用户点头后提交~~ → 已按线拆开提交：`1205609`（切句 + winperf）、`106e57d`（本地兜底 + 记忆状态行）。

### P0 · 启动并验收真实链路
1. 启动 `start-bridge.cmd`，确认状态行是 `memory: enabled ... dry_run=False`。
2. 做一轮真实语音/文件回环，确认能出声、无报错。
3. 看 `~/.voice-ai/memory/sessions/` mtime 是否更新；再启动一轮，确认能读到上一轮摘要（`MEMORY_DEBUG=1` 看 `L2/L4`）。
4. 验收完不要长期挂着；电脑重启后不会自启，需要手动启动。

### P0 · 硬件下一步：联网已通，剩下的是听感与运维
1. 保持 `RECORD_ONLY` 的定稿音频参数，不要回退去调增益/限幅。
2. 联网链路已经通了（`secrets.h` 已建、TURN/TURN_STOP 都上过机），**不要重复建/覆盖 `secrets.h`**，也不要让里面的值出现在终端或提交里。
3. 短回复（1–4 s）听感已经正常。长回复开头的空档已真机定位为「合成第二句慢于第一句音频」，切句定案（首句 ≥18、第二句 ≥20 可逗号切且 26 字上限、其余 ≥18）后真机断流 46 → 0 次（69.1 s 回复）、首字节 4.4–5.2 s；3 s 缓冲不要再加（加过 6 s，无效）。
4. 改接线、烧录、换供电前确认用户在场；不要在未确认时自行改动。

### P1 · 延迟与运维
- 首字节约 4.4–5.2 s（真机，≥18 字首句）；1.5–2 s 目标降级为长期项。主因是 TTS 固定开销（真机约 0.6 s/次 + 110 ms/字；llm/flow/hift 未拆分计时），不是 HTTP；流式合成已实测更慢（§3.13）。
- E（LLM 等待窗口预热 CosyVoice）未做；需要 GPU 授权，且不能与其他 GPU 任务并行。
- `X-Session-Id` 仍是 `d=dev1` 历史 bug；改响应头是可见契约变更，需用户确认。
- bridge 无开机自启/守护；用量账本只是估算，没有硬额度。

### P2 · 记忆与模型
- 中文 bigram 对“猫/养猫”这类查询召回不足；是否加 unigram fallback 要先用真实查询评估。
- `memory_debug.jsonl`、p95 自动回归门未做；当前读路径实测 p50 1.93 ms / p95 2.06 ms，硬门 p95 ≤20 ms。
- 本地 4B/9B 选型仍保留；当前 2B 只用于应急，不承载人格与长期记忆。
- 升级顺序不可跳：HTTP 跑通 → WebSocket（先确认）→ 设备端 VAD/全双工（需 AEC）→ 唤醒词。

### P2 · 后续扩展：屏幕 + 充电宝（未采购、未实现）
- 屏幕走 SPI；首选 3.3V 逻辑的 1.28 寸 GC9A01 圆屏（240×240）或 ST7789 方屏。6 线：SCLK、MOSI、CS、DC、RST、BL，接 GPIO 8–14；RST 避开 GPIO0（已是按键）；不要 RGB 并口屏。
- 电源拓扑（按现有模块修正）：模块只有自带 USB 线（红=5V、黑=GND，**没有 VCC/GND 针**），所以充电宝必须**同时**给模块 USB 和板子 USB 供电，且两路来自**同一个充电宝**（或同一个 USB hub）。两个独立充电宝会失去公共地，I2S 会噪声或无声。
- 充电宝：5V/2A 以上、10000mAh 起、必须带“小电流模式”（否则待机被充电宝判空断电）；**只用 5V USB-A 输出**，不要让多档快充误升 9V/12V 烧 MAX98357A。10000mAh 约 10–15 小时，20000mAh 约 20–30 小时（粗估）。
- 地回路：继续用短 USB 线，模块和板子同 hub / 同充电宝——这是 2026-10-26 把沙沙调干净的关键，不要拆回“两个独立电源”。优先选双 USB-A 口的充电宝；只有一个口就用能带 2A 的 hub。
- 上电前：万用表确认充电宝两个 USB 口的 GND 导通、5V 对地 5.0–5.2 V，再接模块和板子。
- 屏供电从板子 3.3V；屏线/背光远离话筒模拟线；显示是本地功能，不改 bridge 契约；用独立 UI 任务，不阻塞音频；8MB PSRAM 够帧缓冲。
- 当前只在规划阶段：不采购、不改固件、不接线；等联网跑通后再定型号。

## 3. 测试门（照抄，不要改）

```bash
cd C:/Users/DELL/voice-ai/bridge
PYTHONIOENCODING=utf-8 python -m unittest \
  test_memory_session test_memory_store test_memory_extract test_memory_score \
  test_memory_context test_memory_maintenance test_memory_admin \
  test_memory_writer test_memory_integration \
  test_brains test_brain_stream test_server_brain test_usage test_server_usage \
  test_context_builder test_commands test_local_commands test_local_plugin
PYTHONIOENCODING=utf-8 python test_sentence_split.py
PYTHONIOENCODING=utf-8 python test_llm.py
PYTHONIOENCODING=utf-8 python test_pipeline.py
```

必须显式列模块。**不要跑裸 `unittest discover`**：`test_e2e.py`、`test_live.py`、`test_edge_content.py` 是 import 即执行的真实 HTTP/ASR/TTS/云端调用；`test_live.py` 还会启动 CosyVoice、占 GPU、花钱并写账本。
`test_pipeline.py` 是脚本，不是 unittest 模块；把它塞进 unittest 清单会因模块级 monkeypatch 泄漏，出现 `RuntimeError: ollama down` 假失败。

## 4. 安全红线（逐字保留）

- **API key 只在 PC（`~/.deepseek/key`），绝不下发设备；设备只持有 bridge_token（`~/.deepseek/bridge_token`）。**
- **密钥、完整会话记录不写进工作区。**
- **用户说「停/停停」立即停手。**
- **不要重复采购。**
- **除非用户另有指示，不要开始接线、采购或改代码。**
- `~/.deepseek/key` 和 `~/.deepseek/bridge_token` 只能确认存在，不要读取或打印内容。
- `bridge/out/` 录音、`test-data/`、`~/.voice-ai/memory/` 明文都属于个人数据，不要上传、不要贴进公开仓库。

## 5. 关键文件地图

| 位置 | 作用 |
|---|---|
| `bridge/server.py` | HTTP 服务、`/turn`、`/stop`、`/brain`、启动/退出、记忆状态行 |
| `bridge/pipeline.py` | 一轮语音：ASR → LLM/本地命令/本地兜底 → 切句 → TTS → PCM |
| `bridge/brains.py` / `llm.py` | 云端 profile 路由、流式客户端、冷却与探测 |
| `bridge/local_plugin.py` | 本地 Ollama 应急兜底（默认关） |
| `bridge/memory_*.py` | 五层记忆：store/score/extract/context/maintenance/admin/writer |
| `bridge/commands.py` / `local_commands.py` | 规则命令（停/时间/定时） |
| `start-bridge.cmd` | 日常启动入口：开真实记忆 |
| `firmware/voice_terminal/` | ESP32-S3 固件；RECORD_ONLY 与联网两模式都已上机验证（2026-10-27） |
| `HANDOVER-G.md` | 硬件最新交接（接线、烧录、地回路、音频定稿，2026-10-26） |
| `HANDOFF.md` | 权威全量交接（接口契约、验收、PC 端细节） |
| `bridge/README.md` | 桥的实现、性能、测试与运维细节 |

## 6. 已知坑

- Windows + Git Bash：MSYS 会改写 `/quiet`、`X=C:\path` 这类参数；PowerShell 命令用单引号包 `-Command`。
- 控制台编码：跑 Python 前设 `PYTHONIOENCODING=utf-8`，否则中文输出乱码。
- `start-bridge.cmd` 需要 CRLF；用 Git Bash 改写后要检查，LF 会导致 cmd 行为异常。
- D: 是 USB 盘，冷读慢；模型已搬到 C: NVMe，别再搬回去。
- 本地插件必须 `reasoning_effort:"none"` + `keep_alive:0`；否则只出 reasoning 或常驻显存。
- 电脑重启后 bridge 和 Ollama 都不会自启；要手动起。
- 做记忆维护前先备份；不要手删 JSONL。
- 硬件烧录口是 COM5 / CH343，不是 native CDC；XMC flash 的 bootloader patch 在升级 core 后会丢，要重打。
- 串口监视要 `DtrEnable=false` + 手动拉 `Rts`；`arduino-cli monitor` 不会复位。
- 模块只有一根 USB 线（无 VCC/GND 针）；模块和板子必须同电脑/同 hub/同充电宝，地是“借道”的。
- 固件已在 `1a14b2a` 提交；bridge 工作区混着两条线，不要 `git add -A`。

## 7. 接手第一步（建议顺序）

1. `cd C:/Users/DELL/voice-ai && git status --short && git log --oneline -3`
2. 跑上面测试门，确认 379 + 27 全过。
3. `netstat -ano | grep -E ":8080|:11434"` 确认服务是否在跑（当前都不在）。
4. 读 `HANDOFF.md` §1.1–§1.3、§2、§9、§11，以及 `HANDOVER-G.md` 的 §0、§3.2、§3.8、§7。
5. 向用户确认：是否现在提交 PC 端改动、是否进入硬件联网模式。两件事都不要自行决定。
