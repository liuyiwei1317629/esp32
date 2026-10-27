# 语音陪伴项目 · 最终交接（2026-10-23）

> 读者：接手本项目的 AI，或未来的自己。先读第 2 节「硬约束」和第 3 节「接口契约」。  
> 硬件最新状态看 `HANDOVER-G.md`（2026-10-26 定稿 + 2026-10-27 联网补记）。  
> **2026-10-27：联网两模式（TURN / TURN_STOP）已上机验证**，看文首的状态更新。
> 文件清单：第 13 节；维护、修改、扩展：第 14 节。  
> 本文不含密钥、token、完整会话记录；不要把任何密钥或录音内容补进来。

## 2026-10-27 状态更新（优先于下文）

- **硬件已接线上电**：2026-10-26 本地录放链路定稿可用（无沙沙、响度一致、无破音），快照 `firmware/voice_terminal/snapshots/2026-10-26_final-v3/`，参数 `CLIP_TARGET_RMS=2700` / `CLIP_GAIN_MAX=8.0`。**联网两模式 2026-10-27 已上机验证**：TURN 多轮 `result=ok`、`first_pcm_ms` 2.2–4.2 s；TURN_STOP 播放中按 BOOT 打断 `stop_ms=20/18`、`result=aborted`（本地先静音再 POST `/stop`）。当前烧的是 `APP_MODE_TURN_STOP`；打断只在出声后生效（用户当天确认）。硬件事实以 `HANDOVER-G.md` 为准。
- **2026-10-27 上机时改掉两处**：① 删掉开机 6 声诊断音阶（连同 `playDC`/`playSweep` 死代码），只留录音开始那声 60 ms「嘀」——删之前录音全零、删之后 `voice=1`，属经验事实、机制未查清；② 播放缓冲 1 s → 3 s（`PLAY_STREAM_BYTES_MAX=98304`，不够时自动降 2 s / 1 s），长回复断续从中后段大面积补静音缩到只在开头约 1 秒。开口缺口 2026-10-27 真机定位为**开局赤字**（合成第二句比第一句音频慢），固件不用改；改法是首句 ≥18 字、第二句 ≥20 字且允许逗号切（26 字硬上限）、第三句起 ≥18 字，真机 69.1 s 回复 0 次断流、首字节 4.4–5.2 s，见 `NEXT-AI-2026-10-27-tts-latency.md` §3.15。
- **固件与硬件文档已随 `1a14b2a` 提交**：`firmware/README.md`、`audio_io.cpp/.h`、`config.h`、`voice_terminal.ino`、`HANDOVER-G.md`、`NEXT-AI.md`、`snapshots/`。不要回滚或覆盖。
- **屏幕 + 充电宝仅规划，未采购、未实现**；模块是自带 USB 线的成品音箱板，只有一根 USB（红 5V / 黑 GND），没有 VCC/GND 杜邦针。
- **当前供电全部来自电脑**；充电宝/独立电源方案未执行。

## 0. 一句话

PC 端「听 → 想 → 说」已经跑通；云端是唯一的人格/长期记忆大脑，本地只做功能插件。

截至 2026-10-23 的完成度：

| 块 | 状态 |
|---|---|
| A 云端路由 / D 用量账本 | 已完成并接入 |
| 五层记忆阶段 1（纯模块） | 已完成、已验收（148 例） |
| 五层记忆阶段 2（只读接入） | 已完成、已验收（集成 16 例）；默认关闭，不写盘 |
| 五层记忆阶段 3（写路径 dry-run） | 已完成、已验收；阶段 3 当时 writer 18 例、记忆 185 例；默认 `MEMORY_DRY_RUN=1`，不落盘 |
| 五层记忆阶段 4（维护 / 可观测性 / 退出补刷） | **已完成并验收（2026-10-23）**；真实 `~/.voice-ai/memory/` 已于同日晚启用（`start-bridge.cmd`，两轮写入验证） |
| C 规则命令 | **已接入（2026-10-23）**；stop/time/timer 本地短路，不调用 LLM；timer 只说明无法到点提醒 |
| B 上下文模块 | 已交付（63 例），**未接入**；语音链路使用 `memory_context.py`，不要重复接两套 |
| F 固件 | 2026-10-22 三模式编译通过；2026-10-26 硬件实测后以 `RECORD_ONLY` 定稿；**2026-10-27 TURN / TURN_STOP 上机验证通过**（多轮 `result=ok`、打断 `stop_ms` 18–20 ms），同日删开机音阶与死代码、播放缓冲 1 s→3 s |
| G 硬件 | **2026-10-26 已接线上电，本地录放定稿可用；2026-10-27 联网链路已通**；细节以 `HANDOVER-G.md` 为准 |

当前两条主线，互不阻塞：

1. **硬件 bring-up**：按 `handoff/G-hardware-bringup.md` 从断电测量走到 `/stop`。
2. **记忆写路径**：阶段 3/4 已完成，真实目录已于 2026-10-23 启用（启用前目录不存在，无需备份）；日常用 `start-bridge.cmd` 启动，直接跑 `server.py` 仍是关闭 + dry-run。

## 1. 现状

### 1.1 已验证

- `bridge/server.py --serve` 提供 `POST /turn?session_id=<id>` 与 `POST /stop`；请求是完整 WAV（16000 Hz / 16 bit / mono），响应是 chunked PCM s16le/16k/mono，带 `X-Voice`、`X-Gen-Id`、`X-Session-Id`。
- `X-Auth` 已实测：无 token 返回 401。音色走 CosyVoice 主线，失败/超时回落 edge-tts。
- **A 已落地**：`bridge/brains.py` + `bridge/llm.py` 实现 profile 读取、通用 OpenAI 兼容流式客户端、`active`/`fallback`、失败重试、冷却与后台探测；`server.py` 增加 `--brain` 与 `POST /brain`。`~/.voice-ai/brains.toml` 已创建（默认 deepseek，无密钥内容）。
- **D 已落地**：`bridge/usage.py` 按轮把时间、profile、token 数、估算费用、是否 fallback 追加到 `~/.voice-ai/usage.jsonl`；不记录用户原文和密钥。单价来自 profile 的 `price_in_cny_per_mtok` / `price_out_cny_per_mtok`；缺价记 `cost_cny: null`。
- **五层记忆阶段 1 已验收**：`memory_store.py`、`memory_score.py`、`memory_extract.py`、`memory_context.py`、`memory_maintenance.py`、`memory_admin.py` + 8 个测试模块；148 例全过，独立复核后连续 10 轮无失败；L3 打分基准 p50 1.26–1.47 ms、p95 1.42–1.64 ms（空载），负载下 p95 2.1–2.8 ms、max 4.05 ms（500 条 × 200 次查询）。
- **五层记忆阶段 2 已验收（2026-10-23）**：只读接入 `config.py` / `memory_context.py` / `pipeline.py` / `server.py`，新增 `test_memory_integration.py`（16 例）。
  - `MEMORY_ENABLED=0` 时行为与旧代码完全一致：`build_bridge()` 不构造记忆，`/turn` 仍用两参数 `begin(text, timers)`，消息仍是 `system persona + 本轮 user` 两条；`ExplodingStore` 测试证明关闭时不会碰记忆。
  - `MEMORY_ENABLED=1` 时只调用 `memory_context.build_for_turn()`：**不调用 `mark_used`、不写 facts/episodes/sessions、不跑维护、不落摘要/最近轮次**。异常时回退旧两条消息，不伤本轮。
  - 会话桶用 `memory_store.parse_session_id()` 解析出 `dev1`；响应头 `X-Session-Id` 仍是旧的 `d=dev1`，两者互不影响。恶意 `../../evil` 被哈希在 `sessions/` 内，不产生目录外文件。
  - 测试证据：集成 16/16，连跑 10 轮全过；记忆合计 164 例全过（阶段 3 后为 185，阶段 4 后为 195，阶段 5 后为 200）；开启/关闭两种模式均有断言；读取前后文件字节不变、无 `state.lock`。另有两项防护：`state.json` 的 `schema_version` 高于支持值时打印 `WARN ... read-only` 并禁止写入；`MEMORY_*`/端口/价格等数值环境变量非法时打印 `WARN` 并回退默认值。
  - 未做（阶段 2 范围明确排除）：`mark_used`、写队列、`recent_cache`、摘要落盘、抽取、维护、真实积累。**因此阶段 2 的 `hits` 永远是种子值，晋升不会发生。**
- **五层记忆阶段 3 已验收（2026-10-23，dry-run）**：新增 `memory_writer.py`（不可变 `WriteJob`、`RecentCache`、单 worker 队列）与 `test_memory_writer.py`（18 例）；`config.py` 新增 `MEMORY_DRY_RUN=1` / `MEMORY_DEBUG=0`；`pipeline.py` 增加 `Turn.selected_ids` 并把 `recent_cache` 传入读路径；`server.py` 在终块前 `enqueue_memory()`，`run_local`/`record_mic` 同入口。
  - 资格：完整、无 error、非 `length`、`done` 或 `stop`、user/reply 非空；敏感回合（user+assistant）同步拒绝且不进入 `recent_cache`；队列满丢弃，不影响回复。
  - **默认 dry-run 不落盘**：不写 facts/episodes/sessions、不调 `mark_used`，只累计 `dry_run_*`/`hits_deferred`；`recent_cache` 仍在进程内影响下一轮 prompt。真实写入需显式 `MEMORY_DRY_RUN=0`（阶段 4 已在临时目录验收；真实目录已于 2026-10-23 启用，见 §1.1），且 `store.read_only` 时强制 dry-run。
  - 真实写入模式下命中每 5 个完成回合批量 `mark_used` 并只落盘一次，`stop()` 补刷；**维护在阶段 3 未接线**（阶段 4 已接线，见下一条）。
  - 测试证据（阶段 3 当时）：记忆 **185 例全过**（148 + 19 集成 + 18 writer）；hermetic 20 模块 **340 例**；脚本 22/22。集成新增“终块前 cache 可见 + 下一轮读到新记忆”“dry-run 零落盘”“敏感回合零缓存零落盘”。
- **五层记忆阶段 4 已验收（2026-10-23，默认仍 dry-run）**：维护接线、读路径 debug、退出补刷完成；只在临时目录做真实写入验收，**阶段 4 当时未创建/未写入 `~/.voice-ai/memory/`**（同日晚已启用，见 §1.1）。
  - `MemoryWriter(..., maintenance=MemoryMaintenance(store))`：启动从 `state["turns_since_maintenance"]` 恢复增量计数；真实写入模式下启动评估一次，每个成功写盘回合 +1 后调 `maybe_run(completed_turns=count)`，维护运行后归零。计数器是“距上次维护新增回合”，不是会话累计；重启后仍能按 50 回合节流。
  - dry-run 与 `store.read_only` **完全不调用维护**（`maybe_run` 未到期时也会保存 state，dry-run 必须避开这次写盘）；维护异常只计数 `maintenance_errors`，不阻塞写盘与回复。
  - `MEMORY_DEBUG=1` 时读路径打印 `memory: L1=... L2=... L3=... L4=... total=x/budget trimmed=... current=... warnings=...`，只含计数，不含记忆/用户原文；`memory_debug.jsonl` 未实现。
  - 退出补刷：`serve()` 在 `finally` 调 `writer.stop()` 并 `server_close()`（Ctrl+C 也走）；`run_local`/`record_mic` 入队后停止 writer，真实模式会补刷命中。
  - 测试证据：writer **25/25**、集成 **22/22**、记忆 **195 例**；hermetic 20 模块 **350 例**；脚本 22/22。临时目录 3 回合真实写入生成 facts/episodes/sessions/state，`written_jobs=3`、`maintenance_runs=1`、`turns_since_maintenance=3`，真实目录仍不存在。
  - 未做：`memory_debug.jsonl`、`MEMORY_LLM`/`memory_usage.jsonl`、p95 自动回归门（真实目录启用已于 2026-10-23 完成，见 §1.1）。
- **五层记忆阶段 5 已完成（2026-10-23，读路径与缓存上限）**：`active_facts()`/`active_episodes()` 改为只读投影（只复制顶层字段，调用方不得修改嵌套 `history`），`facts()`/`episodes()` 仍深拷贝供维护/管理；`MemoryStore(max_sessions=64)`、`RecentCache(max_sessions=64)`、`MemoryWriter(seen_gen_limit=8192)` 加界（LRU/FIFO）。本机 500 条复测 `active_episodes` p50 3.90→0.10 ms、`build_for_turn` p50 5.72→1.93 ms / p95 6.22→2.06 ms。阶段 5 当时记忆 **200 例**、hermetic **355 例**、脚本 22/22；C 接入后的当前计数见下一条测试环境。未改 `config.py`/`server.py`/`pipeline.py`/`memory_context.py`/固件，默认开关未动；阶段 5 当时真实目录仍未创建（现已启用，见 §1.1）。
- **C 已接入，B 保持归档**：`bridge/commands.py`（54 例）+ `bridge/local_commands.py`（13 例）已接入 `pipeline.Turn`；`turn.local_command` 非空时跳过 LLM、记忆读写与用量账本。`context_builder.py`（63 例）仍不接入，语音链路使用 `memory_context.py`，不要为了“接 B”再改一次消息组装。
- **本地应急兜底已接入（2026-10-23，默认关闭）**：`bridge/local_plugin.py` 走 Ollama OpenAI 兼容 `/chat/completions`，固定功能 prompt，不带人格、不带记忆；只在所有云端 profile 失败且本轮未出字时由 `pipeline.Turn` 调用，已出字不混脑；本地也失败则念固定句「云端暂时连不上，我现在只能做简单的事。」。本地回合跳过记忆写入；账本记 `profile=local`、`cost_cny=null`。默认 `LOCAL_PLUGIN_ENABLED=0`。真实冒烟冷加载 7.53 s、41 字、无 reasoning 泄漏，`keep_alive:0` 后 `ollama ps` 为空、显存回 33 MiB。
- **五层记忆真实目录已启用（2026-10-23 05:42）**：启用前 `~/.voice-ai/memory/` 不存在，因此没有历史数据需要备份。用 `MEMORY_ENABLED=1 MEMORY_DRY_RUN=0` + `--file bridge/out/in_16k.wav --no-cosyvoice`（CPU ASR + edge，不占 GPU）连跑两轮真实回合：第一轮创建 `sessions/939bb46a04c3640c.json`（completed=1）与 `state.json`；第二轮读路径出现 `L2=26`、`L4=1`，确认能取到上一轮摘要。该轮普通陈述句不匹配事实规则（`记住…`/`我叫…`/`我喜欢…` 等），所以 `facts`/`episodes` 仍为空，属抽取规则设计而非故障。此后日常入口是项目根 `start-bridge.cmd`（设两个记忆环境变量后启 `server.py`；直接运行 `server.py` 仍是默认关闭 + dry-run）。启动时会打印一行 `memory: disabled` 或 `memory: enabled dir=... dry_run=...`，可直接确认本次是否真实写入。
- **F 已静态交付并完成编译验证（2026-10-22）**：`firmware/voice_terminal/` 7 个源码/配置 + `firmware/README.md` + `.gitignore`。引脚 4/5/6/15/16/7/0；WAV 44 字节头与 `Content-Length=44+pcm`；`X-Voice: s16le/16000/mono`、chunked/204/401、`/stop` 与 bridge 契约一致；默认两路独立 I2S，24-bit 方案 B 会打印 raw/s16 min/max/RMS。
  - 三模式编译通过（arduino-cli 1.5.1 + `esp32:esp32@3.3.12`，FQBN `esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,PartitionScheme=default,CDCOnBoot=default`）：
    - RECORD_ONLY：377,943 B（28% flash）/ 29,636 B（9% RAM）
    - TURN：935,654 B（71%）/ 48,756 B（14%）
    - TURN_STOP：935,838 B（71%）/ 48,756 B（14%）
  - 2026-10-27 在 10-26 音频重写后重新编译：RECORD_ONLY 385,819 B（29%）/ 52,196 B（15%）、TURN 943,602 B（71%）/ 71,316 B（21%）、TURN_STOP 943,782 B（72%）/ 71,316 B（21%），0 error；当时自有文件只剩 `playSweep`/`playDC` 两条未使用告警。
  - 2026-10-27 收尾清理后重编：RECORD_ONLY 397,319 B（30%）/ 52,316 B（15%）、TURN 943,302 B（71%）/ 71,316 B（21%）、TURN_STOP 943,486 B（71%）/ 71,316 B（21%），**0 error / 0 warning**（删掉了 `playDC`/`playSweep` 与全部临时诊断打印）。
  - `--warnings all` 下自有文件 0 warning；`compile_commands.json` 确认 `-DAPP_MODE=1/2` 生效。2026-10-26 上机完成本地录放，**2026-10-27 联网两模式上机验证通过**（见 §1.2 开头）。
  - 2026-10-22 复审后修了 6 处：`s_waitRelease` 重复触发、播放排空 abort 卡死、`/stop` 先本地 flush、排空阶段可打断、上传提前响应/超时、`MIC_AMP_SHARED_CLOCKS=1` 编译守卫。单总线路径仍未实现，只是被禁止。
- **测试环境口径**：脚本自检 29/29（sentence_split 10、llm 9、pipeline 10）。**单元回归必须用显式模块清单**：19 个 unittest 模块共 **381 例全过**（2026-10-27 实测），其中记忆 204 例；加上 3 个脚本自检，hermetic 合计 **22 个测试文件、410 例**。`test_e2e.py`、`test_live.py`、`test_edge_content.py` 是脚本式实跑，**import 即执行真实 HTTP / ASR / TTS / 云端调用**（`test_live` 还会启动 CosyVoice 占 GPU、花钱并写用量账本），不是 hermetic 单测。**不要跑裸 `unittest discover`。** 系统 Python 缺可选依赖 `funasr`，导入这些实跑脚本会报缺依赖；那是环境问题，不是记忆回归。`test_e2e.py` 不带 `X-Auth`，本机有 `bridge_token` 时直接跑会 401；要跑就按 G 手册 §4 用空 token + 不存在的 token 文件的环境变量，且它仍会启动真 ASR/edge，不能当单元回归门。
- 性能基线（以 `bridge/README.md` 实测节为准）：首字节常见 4.4–5.2 s（2026-10-27 切句定案：首句 ≥18 字、第二句 ≥20 字可逗号切且 26 字硬上限、其余 ≥18 字；真机 69.1 s 回复 0 次断流，22.8–81.4 s 共七轮 0 次，修前 46 次）；CosyVoice 快态 125–215 ms/字，慢态可到 300–558 ms/字，重启后恢复；DeepSeek TTFT 0.1–1.0 s；ASR 在 CPU 上约 0.2–0.5 s。
- `~/.deepseek/key` 与 `~/.deepseek/bridge_token` 已配置；只能确认存在，不要读取或打印内容。
- 根 `README.md` 与 `bridge/README.md` 的「云端为主 + 本地功能插件」节已同步；记忆与固件的最终口径以本文和 `bridge/README.md` 记忆节为准。

### 1.2 还差什么（按优先级）

**P0 · 硬件联网（已完成 2026-10-27）**

> 2026-10-27 结果：TURN 多轮 `result=ok`（`first_pcm_ms` 2.2–4.2 s；整轮墙钟 5–61 s）。**日志字段名有歧义，读的时候别望文生义**：串口的 `upload_ms` 实际是**整轮墙钟**（上传+等待+播放+排空，`millis()-t0`），`play_ms` 是**回复音频时长**（`pcm_bytes/32`，不是播放耗时）。bridge 侧识别正常；TURN_STOP 播放中按 BOOT 打断两次 `stop_ms=20/18`、`result=aborted`，本地先静音再 `POST /stop`。等待首字节期间按键按设计不响应。
>
> 上机时改了两处：① **删掉开机 6 声诊断音阶**（连同注释与死代码 `playDC`/`playSweep`），删之前 4 次录音全零、删之后每次 `voice=1`——**经验事实，机制未查清**；② **播放缓冲 1 s → 3 s**，长回复断续从中后段大面积补静音缩到只剩零星缺口——干净版实测 55.7 s 回复 `underruns=46`（≈1.47 s 静音、2.6%），修之前 7.1 s 回复就有 59 次（≈1.89 s、27%）。残余缺口全在回复开头，2026-10-27 真机逐句遥测定位为**开局赤字**：缺口 ≈ 合成(第二句) − 音频(第一句)。定案：首句 ≥18 字、第二句 ≥20 字（允许逗号切）、其余 ≥18 字，第二句另加 26 字硬上限；真机 69.1 s 回复 0 次断流，72.5 s 那轮（加上限前）5 次（0.16 s），其余五轮 0 次；首字节 4.4–5.2 s（见 `NEXT-AI-2026-10-27-tts-latency.md` §3.15）。
>
> 下面 1–6 是 2026-10-26 初始上机的历史记录，保留备查。

1. 到货确认、开箱照片、N16R8/模块 5 V 丝印核对。
2. 断电测 VCC–GND 短路、`SCK↔BCLK`、`WS↔LRCLK`、喇叭阻抗/对地。
3. 空载测外部 5 V（5.0–5.2 V）与极性；共地；绝不从开发板 5V 引脚给模块供电。
4. 装 WCH CH343SER，确认 COM 口；按 RECORD_ONLY → TURN → TURN_STOP 烧录。
5. 实测 24-bit 转换（`MIC_SHIFT`）、VAD 阈值（`REC_SILENCE_RMS`）、160 ms 预缓冲/underrun、按键打断时序。
6. 记录首字节、桥端 `FIRST_AUDIO_MS`、`/stop` 静音延迟。

**P0 · 记忆真实写入（已完成 2026-10-23）**

- 阶段 4 已接线维护、读路径 debug、退出补刷，并在临时目录完成 `MEMORY_DRY_RUN=0` 真实写入验收；**真实目录已于 2026-10-23 用 `start-bridge.cmd` 启用并完成两轮写入/读取验证**。代码默认仍是关闭 + dry-run（避免测试与实跑脚本污染真实档案）。
- dry-run 下 `recent_cache` 只在本进程内影响下一轮 prompt；摘要/事实/待办不会积累，`hits` 不增长，“注入 3 次晋升”不会触发。
- 维护代码已可用：真实写入模式下启动/每 24 h/每 50 个完成回合触发晋升、降级、容量清理；dry-run 下不调用（避免写 state）。`memory_debug.jsonl` 仍未实现，`MEMORY_DEBUG=1` 只打 stdout 计数。
- 已完成：真实目录启用、两轮写入/读取验证（2026-10-23）。后续维护前先备份 `~/.voice-ai/memory/`；删除走 `memory_admin.py`，不要手删 JSONL。
- 记住：事实/情节是规则抽取，只有「记住…」「我叫…」「我喜欢…」等句式才会进 `facts`；普通聊天只更新会话摘要与最近轮次，这不是漏写。

**规则命令（C，已完成）**

- `pipeline.Turn` 在调用 LLM 前先跑 `parse_command`；命中后由 `local_commands.reply_for` 生成本地文本，直接进入原有句子切分/TTS/流式播放，不调用云端。
- `stop` → `好。`；`time` → 本地时间（上午/下午/晚上 + 中文数字）；`timer` → 确认时长/标签，并明确“现在还不能到点主动出声”。
- 本地命令回合 `turn.local_command` 非空：`server.enqueue_memory` 跳过 writer，不写记忆、不进 `recent_cache`、不写用量账本。
- 定时提醒仍受请求/响应契约限制，没有线程/定时器；要真正到点提醒需设备端长轮询/WebSocket 或本地蜂鸣器。
- 已修 `五分钟后别叫我` 误判：否定模式补 `(?:别|不要|不用)叫我`，回归在 `test_commands`。

**P1 · 运维与成本**

- bridge 没有开机自启/守护；电脑重启后要手动 `server.py --serve`。
- 用量账本只是估算，没有硬性额度/自动停机；额度控制仍靠控制台。
- 8080 入站防火墙改动属系统级变更；连接失败只报告，不改规则。
- `X-Session-Id` 仍是 `d=dev1`（`server._session()` 的历史 bug）；记忆桶已独立解析，但修头值是可见契约变更，需用户确认。

**P1 · 延迟**

- 首字节：切句定案后真机 4.4–5.2 s（原 6–8 字首句为 2.0–2.5 s）；换来长回复开局不再掉空档（真机修前 46 次 → 69.1 s 回复 0 次）。1.5–2 s 目标降级为长期项，瓶颈是 TTS 固定开销（真机约 0.6 s/次 + 110 ms/字），不是 HTTP。
- E（LLM 等待窗口预热 CosyVoice）未做；需要 GPU 授权且不能与其他 GPU 任务并行。
- CosyVoice 慢态靠重启恢复；不要用换引擎掩盖机器状态。

**P1 · 本地兜底**

- 已有本地应急兜底，但**默认关闭**且只做简单问答：Ollama 未启动、超时或空正文时只念固定句；不承载人格与长期记忆。打开前先启动 Ollama；PC 关机仍然完全不可用。
- 模型在 C: NVMe（`C:\Users\DELL\.ollama\models`），冷加载约 5.7 s、显存 2.36 GB；`keep_alive:0` 每轮卸载。`D:\ollama\models` 旧副本已于 2026-10-23 经用户确认删除；`D:\ollama\ollama-windows-amd64.zip`（1.58 GB）仍在。

**P2 · 未来升级（顺序不可跳）**

1. 保持 HTTP，把板子跑通（当前）。
2. WebSocket 流式上行/下行（**收益已重估为可疑**：2026-10-27 实测 TTS 流式更慢、首字节主因是单次合成固定开销；真要做得先证传输是瓶颈；先向用户确认契约变更）。
3. 设备端 VAD/全双工打断（必须配 AEC；服务端 VAD 切句不等于设备端打断）。
4. 唤醒词（最后）。
- UDP 只是局域网优化项，不是 MVP 前置条件。
- 多设备前要先做 `/stop` 的 session 作用域；当前是全局打断。

**P2 · 屏幕与便携供电（2026-10-27 记录，未采购、未实现）**

- 屏幕走 SPI：首选 3.3V 逻辑的 1.28 寸 GC9A01 圆屏（240×240）或 ST7789 方屏；6 线 SCLK/MOSI/CS/DC/RST/BL 接 GPIO 8–14，RST 避开 GPIO0（按键）；不要 RGB 并口屏；屏从板子 3.3V 取电。
- 电源拓扑（按现有模块修正）：模块只有自带 USB 线（红 5V / 黑 GND，**没有 VCC/GND 针**），所以充电宝必须**同时**给模块 USB 与板子 USB 供电，且两路来自**同一个充电宝**或同一个 USB hub。两个独立电源会失去公共地，I2S 会噪声或无声。
- 充电宝：5V/2A 以上、10000mAh 起、**必须带“小电流模式”**，否则待机时会被判空断电；**只用 5V USB-A 输出**，避免多档快充误升 9V/12V 烧 MAX98357A。10000mAh 约 10–15 小时、20000mAh 约 20–30 小时（粗估）。
- 地回路：继续用短 USB 线，模块和板子同 hub / 同充电宝——这是 2026-10-26 把沙沙调干净的关键；不要拆回“两个独立电源”。优先选双 USB-A 口的充电宝；只有一个口就用能带 2A 的 hub。
- 上电前：万用表确认充电宝两个 USB 口的 GND 导通、5V 对地 5.0–5.2 V，再接模块和板子。
- 屏线/背光远离话筒模拟线；显示是本地功能，不改 bridge 契约；用独立 UI 任务，不阻塞音频；8MB PSRAM 够帧缓冲。
- 等联网跑通后再定型号；当前不采购、不改固件、不接线。

**P2 · 记忆质量与隐私**

- 中文 bigram 对单字/跨字查询（“猫”“养猫”）召回不足；阶段 3/4 未改，留后续用真实查询评估是否加 unigram fallback。
- 整条读路径：阶段 5 前的基线是独立复核 p50 6.1–6.4 ms、本机高相似数据 p50 11.5–13.0 ms；**阶段 5 改为只读投影后本机 500 条复测 p50 1.93 ms / p95 2.06 ms**（`active_episodes` p50 0.10 ms）。**硬门 p95 ≤20 ms**，<5 ms 目标已达成；剩余仅自动回归门。
- `MemoryStore._sessions`、`RecentCache._items`、`MemoryWriter._seen_gens` 均已在阶段 5 加界（64/64/8192，LRU/FIFO），不再随进程生命周期无界增长。
- `test_memory_integration.py` 的 2 处 `d=dev1` 硬断言已在阶段 3 改为契约断言；修正 header 时不再误报。
- 记忆是 `~/.voice-ai/memory/` 下的明文短句；没有加密、自动备份、保留期执行。`bridge/out/` 与 `test-data/` 有真人录音，属于个人数据，不要上传。

**P2 · 测试环境**

- 系统 Python 缺 `funasr`，导入 `test_e2e.py` / `test_live.py` 会失败；`.venv` 才有 ASR 依赖。**不要用裸 `unittest discover`**：这两个是 import 即执行的脚本。`test_e2e.py` 按 G 手册 §4 的空 token 环境跑（真 ASR/edge，不占 GPU）；`test_live.py` 会花真实 API、启动 CosyVoice 占 GPU 并写账本，**没有明确授权不要跑**。

### 1.3 硬件状态（历史：截至 2026-10-23；当前见顶部更新与 `HANDOVER-G.md`）

| 件 | 价格 | 状态 |
|---|---:|---|
| ESP32-S3 N16R8 开发板（已焊排针 + 天线） | ¥26.5 | 已购；是否在手以用户确认为准 |
| 40P F-F 杜邦线 21 cm | ¥9.56 | 已购；是否在手以用户确认为准 |
| 鹿小班 ICS43434 + MAX98357 集成音频模块（含喇叭） | ¥24.2 | 预计 2026-10-23 到；**未确认到货** |
| USB-A → 2P 母杜邦 300 mm 24 AWG 电源线 | ¥5.9 | 预计 2026-10-25 到；若已付，总价 ¥66.16 |

- **模块 VCC 是 5 V 标称**：板载 HT33/HT7533 系列 3.3 V LDO 只给 ICS43434 话筒；MAX98357A 直接吃 VCC。不要用 3.3 V 当设计电压。
- 模块引脚从左到右：**SD、SCK、WS、DIN、BCLK、LRCLK、GND、VCC**；`SPK` 接喇叭。
- 默认按**两路独立 I2S**：话筒 SD/SCK/WS，功放 DIN/BCLK/LRCLK。到货先测 `SCK↔BCLK`、`WS↔LRCLK` 是否导通；若导通，说明模块内部共时钟，必须停手报告并改单总线方案，不能把两组时钟分别接到两个 GPIO 输出。
- 供电：外部 5 V（J-120W 充电器，标 5V3A/9V2A/11V1.5A/12V1.5A，实际最大 18 W；“120W”是型号名）；模块与开发板**共地**。**绝不能从开发板的 5V 引脚给模块供电。**
- 白色 USB-A→USB-C 6A 线可用于 CH343/UART 口供电与烧录，但 6A 是额定值，不代表一定带数据；看到 COM 口才算数据线通过。不要剪线。
- 不要重复采购。**未收到用户“零件到了，开始接线”的明确指令前，不接线、不烧录、不采购。**

## 2. 硬约束（逐字保留）

- **API key 只在 PC（`~/.deepseek/key`），绝不下发设备；设备只持有 bridge_token（`~/.deepseek/bridge_token`）。**
- **密钥、完整会话记录不写进工作区。**
- **契约变更（加字段/端点）先向用户确认，并同步 bridge、固件、README 与测试。**
- **用户说「停/停停」立即停手。**
- **不要重复采购，不要改 bridge 代码，不要跑 GPU 任务。**
- **除非用户另有指示，不要开始接线、采购或改代码。**

本交接额外约束：

- B/C/F 包**只允许新建指定文件**；不得修改、删除、重命名任何已有文件。
- B/C/F/G **不得修改** `bridge/config.py`、`bridge/llm.py`、`bridge/pipeline.py`、`bridge/server.py`。集成只由本会话做。
- B/C 的测试**只用标准库**，不联网、不调用模型、不读写密钥、不占 GPU。
- F 的代码不得包含真实 Wi-Fi 密码、bridge_token、API key；只放 `secrets.example.h` 占位符。
- G 不改防火墙（8080 入站变更属系统级）、不采购；起 GPU 任务前先问用户；先征得用户同意再装驱动或工具链。
- 记忆阶段 3/4 默认 dry-run；真实目录已于 2026-10-23 启用（`start-bridge.cmd`）。改记忆或删数据前先备份；不要把 `config.py` 的默认值改成开启。

## 3. 接口契约（不要改）

| 项 | 约定 |
|---|---|
| 监听 | `0.0.0.0:8080`；局域网地址 `192.168.0.100:8080` |
| 对话 | `POST /turn?session_id=<id>` |
| 请求体 | 完整 WAV：16000 Hz / 16 bit / mono；必须带 `Content-Length` |
| 鉴权 | 头 `X-Auth: <bridge_token>`；无 token 或错误返回 401 |
| 响应 | HTTP chunked PCM，s16le / 16000 Hz / mono；头 `X-Voice: s16le/16000/mono`、`X-Gen-Id`、`X-Session-Id` |
| 空识别 | 返回 204，无音频 |
| 打断 | `POST /stop`，带 `X-Auth`；全局打断当前代次，body 可为空 |
| session_id | 服务端截断到 55 字符；设备固定 `dev1`；记忆桶用独立解析器 |
| 加密 | 无 TLS；只在局域网使用，不暴露公网 |
| 起始水位 | 设备端先缓冲 150–200 ms PCM 再开 I2S，避免 underrun（固件当前 160 ms） |
| 打断语义 | 设备收到 `/stop` 或本地按键后**本地 flush**；服务端可能还在等当前句合成完，连接晚关几秒不是可听延迟 |
| 切脑 | 一旦向 TTS 送出内容，不在回复中途切换；下一轮再切 |
| 记忆 | 不新增端点/字段；写路径由客户端启动开关控制（`start-bridge.cmd` 开启，代码默认 dry-run）；当前响应头 `X-Session-Id` 仍是 `d=dev1`，待用户决定是否修 |

`/turn` 的处理顺序是：读 WAV → SenseVoice → 云端 LLM 流式 → 切句 → TTS → chunked PCM。云端 TTFT 不是主要瓶颈，首句字数 × 合成速度才是。

## 4. 角色边界

- **云端**：人格、情绪、长期记忆、连续对话、哲学思辨。所有会写进人格记忆的内容只走云端。
- **本地**：功能插件，不参与人格。只做命令/意图识别、时间/计时、离线小工具、短摘要；不生成陪伴式回复，不写人格记忆，不冒充同一个人。
- **云端不可用**：明确告知「现在只能做简单的事」，本地执行功能命令；恢复后回云端。离线期间最多记一条「离线时段」，不灌回人格。
- **不带 DSH 上下文和工具**：只表示不把 DSH 的系统提示、工作区文件、工具定义、完整会话历史塞进语音请求；**不等于没有长期记忆**。长期记忆存外部，按需检索。
- **最近两轮**：当前这轮之前、已经完整结束的 2 轮（一问一答算一轮）。空识别、被打断、没有完整回复的轮次不算。当前正在生成的回复不在历史里。
- **每轮上下文**：人格 ≤120 token；top-3 长期记忆（每条 ≤30 字）；最近 2 轮 + 摘要；本轮用户输入 ≤500 字。**硬上限 1050 token/轮**（五层记忆设计默认），约 530 token 是常态目标，不是硬限制；超预算时按 摘要→记忆→历史 裁剪，人格与本轮输入不砍。
- **开机问候音**：只在启动时播放，属于人格资产，可替换；不是每轮垫音，不优化首字节。

## 5. 文件所有权

| 角色 | 可以动 | 不可以动 |
|---|---|---|
| 本会话（集成 + A/D/E/H + 记忆） | `bridge/config.py`、`bridge/llm.py`、`bridge/pipeline.py`、`bridge/server.py`；记忆模块与测试；A/D/E 文件；本文档 | B/C/F/G 的交付文件 |
| B 包 | 新建 `bridge/context_builder.py`、`bridge/test_context_builder.py` | 其他一切已有文件 |
| C 包 | 新建 `bridge/commands.py`、`bridge/test_commands.py` | 其他一切已有文件 |
| F 包 | 新建 `firmware/**` | `bridge/**` 与其他已有文件 |
| G 包 | 只读、测量、报告；日志放对话里 | 不改代码、不改防火墙、不采购 |

**注意**：上表只是分工，不是改码许可。A/D、记忆阶段 1–5、C 集成已获用户明确授权并完成；E 仍需 GPU 授权与范围确认。记忆真实目录已于 2026-10-23 经用户确认启用。

`bridge/out/` 里有真人录音和合成音频，属于个人数据；不要上传、不要贴进公开仓库。

## 6. 施工顺序（2026-10-23 起）

1. **A（已完成 2026-10-22）**：profile 路由 + fallback；未碰 GPU。
2. **D（已完成 2026-10-22）**：用量账本。
3. **记忆阶段 1（已完成 2026-10-23）**：六个纯模块 + 148 例。
4. **记忆阶段 2（已完成 2026-10-23）**：只读接入 + 16 例；默认关闭。
5. **硬件 bring-up（与 6 可并行）**：按 G 手册；这是唯一能证明设备可用的路径。
6. **C 集成（已完成 2026-10-23）**：先修否定误判，再定 `stop`/`time`/`timer` 的本地执行语义；`timer` 只确认、不假装会主动提醒。
7. **记忆阶段 3（已完成 2026-10-23）**：`memory_writer.py` + 不可变 `WriteJob` + `recent_cache` + 敏感预检 + dry-run + `mark_used` 批量刷；默认不落盘。
8. **记忆阶段 4（已完成 2026-10-23）**：维护接线、读路径 debug、退出补刷；临时目录真实写入验收；同日晚真实目录启用并完成两轮验证。
9. **E（需 GPU 授权）**：LLM 等待窗口预热 CosyVoice；不与任何 GPU 任务并行。
10. **未来**：WebSocket → 设备端 VAD/AEC → 唤醒词；顺序见 6.1。
11. **B**：保留为参考/归档；语音链路已由 `memory_context.py` 覆盖，**不要重复接入**。

不要先做 E 再做 A：E 只省 0.3–0.5 s，A 决定云端不可用时还能不能用。

### 6.1 升级路线（用户 2026-10-22 定，按此顺序，不得跳步）

| 序 | 阶段 | 收益/前提 | 不要提前做的事 |
|---|---|---|---|
| 1 | **保持 HTTP，先把板子跑通** | 唯一能验证 24-bit 移位、I2S 电平、供电、PSRAM 的路径；G 段全部工作都在这里 | 不要在板子跑通前改状态机、换传输、加模型 |
| 2 | **传输/流式（已重估，不再是首选）** | 2026-10-27 实测 CosyVoice `stream=True` 首块 2.84–3.60 s、块到达 RTF 1.09–1.63，比整句合成还慢，**流式拿不到收益**；首字节的主因是 TTS 单次调用约 1.9 s 固定开销，不是 HTTP（见 `NEXT-AI-2026-10-27-tts-latency.md` §3.11–§3.13） | 不要为了「流式」改契约——先证明传输不是瓶颈 |
| 3 | **加 VAD / 打断**（需要 AEC） | 设备侧麦克风与喇叭同板紧贴，回声路径强；没有 AEC 做不了全双工打断 | 不要用「关麦 + 定时」假装全双工 |
| 4 | **最后才考虑唤醒词** | 唤醒词解决的是「不用手也不误触发」，优先级低于对话本身自然度 | 不要在 2/3 之前占 Flash 给唤醒词模型 |
| — | UDP（局域网优化项，**非必需**） | 只在第 2 步的 WebSocket 已嫌重、且局域网丢包/延迟成为实测瓶颈时才评估 | 不要把它当 MVP 前置条件 |

补充事实（2026-10-23 核对）：

- 设备端目前是**按住说话**：`voice_terminal.ino` 只在按键按下时进 RECORDING；结束条件是「松手 / 静音 1.2 s / 最长 15 s」。每轮都要再按一次，说话时不能松手。
- 免按键属于第 3 步，不是第 1 步；最小可用形态是「播放期间关麦 + 播完自动开麦」的半双工循环（不需要 AEC），但**不允许**把它说成支持打断。
- 现 `REC_SILENCE_RMS=500` 仍是未实测的拍脑袋值；第 1 步的串口 rms 日志要先攒够，再谈阈值或换模型 VAD。
- 第 2 步的服务端形态未定（WebSocket 端点、帧格式、会话保持），落地前需先把契约变更报给用户，并在第 3 节留档。
- **两个 VAD 不是同一个东西**：第 2 步里的「VAD 切句」是**服务端**断句（判一段话说完、喂给 ASR），不需要 AEC；第 3 步的 VAD 是**设备端**全双工打断（播放中判断用户插话），必须配 AEC。
- **WS 的收益不在协议本身**：瓶颈是 TTS 合成速度（CosyVoice 快态 125–215 ms/字）和「录完整段才开始」这两段等待。换 WS 缩短的是后者；前者要靠首句更短或更快引擎。
- 同一份路线在 Qoder 项目记忆里另有副本：`C:\Users\DELL\.qoder-cn\projects\C--Users-DELL-Documents-deepseek-harness-default-workspace\memory\project-esp32-voice-kit.md`（`Upgrade roadmap` 一节）。数字口径以 `bridge/README.md` 实测节为准。

## 7. 外包包与模型（归档）

| 包 | 主写 | 复核 | 说明 |
|---|---|---|---|
| B 上下文/记忆 | Qwen3.8-Flash 思考中（0.65x） | DeepSeek V4.1 Flash max（夜间 0.11x） | 已交付 63 例；未接入，作为归档 |
| C 规则命令 | DeepSeek V4.1 Flash max | 单测裁决 | 已交付 54 例 + 本地回复 13 例；**2026-10-23 已接入** |
| F 固件骨架 | Qwen3.8-Flash 极致（1.20x） | DeepSeek V4.1 Flash max | 已交付；2026-10-26 以 `RECORD_ONLY` 定稿，联网待验证 |
| G 硬件调试 | Qwen3.8-Flash 思考中，且必须能读图 | 用户照片 + 本会话复核 | 使用 `handoff/G-hardware-bringup.md` |

- 倍率只代表价格，不代表能力；以上按任务风险分配。
- 四份提示词：`handoff/B-context-memory.md`、`handoff/C-rule-commands.md`、`handoff/F-firmware-skeleton.md`、`handoff/G-hardware-bringup.md`。B/C/F 已交付，提示词转为归档；G 仍是现役 runbook。

## 8. 集成接口

### A · 云端路由（已实现）

- `llm.stream_reply(messages, on_content, abort=None)` 签名不变，`pipeline.py` 调用点未改。
- `bridge/brains.py`：TOML 解析、profile 校验、`BrainRouter`（active/fallback、重试、冷却、探测、pinning）；`bridge/llm.py` 保留 `consume()` 并新增通用流式客户端 `stream_profile()` 与路由单例。
- 配置读 `~/.voice-ai/brains.toml`；`key_file` 按 profile 指定，默认 deepseek 复用 `~/.deepseek/key`，也可放 `~/.voice-ai/keys/<profile>`；密钥不进工作区。
- 失败规则：首 token 3 s 超时、连接失败、5xx/408/425 重试一次；401/403/402/429 不重试，直接下一个；一旦 `on_content` 已送出内容，本轮不再换脑。
- 冷却 5/15/30 分钟；配置里的 primary 失败后由后台线程探测，成功自动恢复；显式 `/brain` 切换视为 pin，不自动切回。
- `/brain` 只接受本机/私网来源，带 `X-Auth`，返回 `active`/`pinned`/`cooling`，下一轮生效。

### D · 用量账本（已实现）

- `bridge/usage.py`：`record_turn(stats, price_lookup=llm.profile_prices)` 读取 A 的 `usage` 与 `profile`，追加到 `~/.voice-ai/usage.jsonl`。
- 每行字段固定为 `ts`（UTC ISO 8601）、`profile`、`prompt_tokens`、`completion_tokens`、`cost_cny`、`fallback_used`；不记录用户原文、模型输出、密钥或 base_url。
- 单价写在 `[brain.profiles.<name>]`：`price_in_cny_per_mtok`、`price_out_cny_per_mtok`（元/百万 token）。不填则 `cost_cny` 为 `null`，不伪造 0 元。
- 默认 deepseek 用高峰价 2 / 8 保守估算；空闲/缓存命中会更低，所以账本高于实际账单，不是账单本身。
- 记录发生在响应终止块之前；账本写入失败只打印 WARN，不打断音频返回。fake LLM 的 `test_e2e.py` 没有 profile，不会写账本。

### 五层记忆（阶段 1–5 已实现；真实目录已启用，代码默认关闭 + dry-run）

- 实际入口：
  ```python
  messages, info = memory_context.build_for_turn(
      store, config.DEFAULT_PERSONA, session_id, user_text,
      budget_tokens=config.MEMORY_INPUT_BUDGET_TOKENS,
      token_cache=bridge.token_cache,
      recent_cache=recent_cache,
      current_input_max_chars=config.MEMORY_CURRENT_INPUT_MAX_CHARS,
  )
  ```
  `recent_cache` 来自 `bridge.memory_writer.recent_cache.get(session_id)`（writer 关闭时为 `None`）。
- `pipeline.Turn` 在 `MEMORY_ENABLED=1` 且 `bridge.memory_store` 存在时走上述入口；异常或 `info["fallback"]` 时回退 `[system persona, user text]`。实际注入的 id 存入 `turn.selected_ids`。
- `Bridge(tts, memory_store=None, token_cache=None, memory_writer=None)`；`Bridge.begin(..., session_id="anon")`。
- `server.build_bridge()` 只在启用时构造 `MemoryStore`、`TokenCache` 与 `MemoryWriter`；`/turn`、`run_local`、`record_mic` 在启用时传解析后的 session，关闭时保持旧两参数调用。
- 代码默认（直接跑 `server.py`）仍是关闭 + dry-run：不构造 writer，不写 facts/episodes/sessions。**真实写入只在 `start-bridge.cmd` 下发生**（`MEMORY_ENABLED=1`、`MEMORY_DRY_RUN=0`），2026-10-23 已完成两轮真实写入/读取验证。
- 写入入口是 `server.enqueue_memory(bridge, turn)`：`/turn` 在 PCM 循环后、终块前调用；`run_local`/`record_mic` 在 PCM 循环后调用，随后 `stop_memory(bridge)` 补刷。任何异常只打印 WARN。
- 真实写入（`MEMORY_DRY_RUN=0` 且 store 非只读）才启用维护与命中落盘；`serve()` 退出时 `writer.stop()` + `server_close()`。真实目录启用前若无历史数据可直接启用；**已有数据后要改结构或清理，先备份 `~/.voice-ai/memory/`**。

### C · 规则命令（已接入 2026-10-23）

```python
from commands import parse_command
from local_commands import reply_for

cmd = parse_command(text)   # -> Command | None
reply = reply_for(cmd)      # -> str | None；只生成文本
```

- `Command.kind` 只允许 `"stop"`、`"time"`、`"timer"`。
- `pipeline.Turn._handle_local_command()` 在组装消息、调用 LLM 之前短路；命中后文本仍走原有切句/TTS/流式播放，`turn.local_command` 记录 kind。
- `local_commands.reply_for` 不联网、不读写记忆、不起线程、不调用 TTS。`stop` → `好。`；`time` → 本机时间（上午/下午/晚上 + 中文数字）；`timer` → 确认时长/标签。
- `server.enqueue_memory` 对 `local_command` 非空回合直接跳过：不写记忆、不进 `recent_cache`、不写用量账本。
- **定时提醒的限制**：当前 HTTP 契约是请求/响应，回合外无法主动出声。`timer` 只回复「我知道你想在 X 后提醒你……但现在还不能到点主动出声」；要真正提醒，需要设备端增加长轮询/WebSocket，或本地蜂鸣器。不要用睡眠线程直接调 TTS 绕开设备播放路径。
- 已知否定误判已修：`五分钟后别叫我` 不再解析成 timer，回归在 `test_commands`。

### B · 上下文（已交付，归档）

- `bridge/context_builder.py` 仍是独立纯模块（63 例），但语音链路使用 `memory_context.py`。两者都做消息组装，**不要同时接入**；B 仅作为历史参考或非语音场景的备用。
- B 的设计上限同时取满约 1300 token，已由 `memory_context` 的 1050 硬预算取代。

### E · TTS 预热

- 在 LLM 首 token 等待窗口（0.3–0.6 s）让 CosyVoice worker 做一次极短预热，掩盖 DVFS 爬升。
- 只在用户授权 GPU、且没有其他会话占卡时做；不得与 `throttle_probe.py` 或 `test_live.py` 并行。

## 9. 验收命令

在 `C:\Users\DELL\voice-ai` 下执行。

```bash
# 1) 记忆全部单测（阶段 1–5 + 本地命令记忆跳过契约），无网络、无 GPU
cd bridge
../.venv/Scripts/python.exe -m unittest -v \
  test_memory_session test_memory_store test_memory_extract test_memory_score \
  test_memory_context test_memory_maintenance test_memory_admin \
  test_memory_writer test_memory_integration

# 2) 旧脚本自检
cd ..
.venv/Scripts/python.exe bridge/test_sentence_split.py
.venv/Scripts/python.exe bridge/test_llm.py
.venv/Scripts/python.exe bridge/test_pipeline.py

# 3) 现有 unittest（A/D/B/C；无网络、无 GPU）
cd bridge
../.venv/Scripts/python.exe -m unittest -v test_brains test_brain_stream test_server_brain test_usage test_server_usage
../.venv/Scripts/python.exe -m unittest -v test_context_builder test_commands test_local_commands test_local_plugin
cd ..
```

端到端（假 LLM + 真 ASR + edge，不占 GPU）：

```bash
# test_e2e.py 不发 X-Auth；本机已有 bridge_token 时会 401。
# 用不存在的 token 文件跑 127.0.0.1:8088，不要改测试代码。
BRIDGE_HOST=127.0.0.1 BRIDGE_PORT=8088 \
BRIDGE_TOKEN= BRIDGE_TOKEN_FILE=C:/Users/DELL/no-such-token \
  .venv/Scripts/python.exe bridge/test_e2e.py
```

固件三模式编译（工具链已验证：arduino-cli 1.5.1 + `esp32:esp32@3.3.12`）：

```bash
D:/arduino-cli/arduino-cli.exe --config-file D:/arduino-cli/arduino-cli.yaml compile \
  --fqbn 'esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,PartitionScheme=default,CDCOnBoot=default' \
  --build-property 'build.extra_flags=-DAPP_MODE=0' \
  --output-dir D:/arduino-build/final-record firmware/voice_terminal

# 1=TURN，2=TURN_STOP；把 APP_MODE 改掉再跑。
# 上传：把 compile 换成 compile --upload -p COMx，或按 G 手册操作。
```

- `secrets.h` 由用户从 `secrets.example.h` 复制后手动填；不要打印、提交、上传真实值。
- 真实全链 `test_live.py` 会花两轮 DeepSeek 调用并占 CosyVoice GPU；只有在用户明确同意、且没有别的会话占卡时才跑。`throttle_probe.py` 同样占 GPU。
- G 的验收不是命令，而是检查点：电压 → 导通 → COM 口 → 录音回放 → HTTP 一轮 → `/stop` → 延迟记录。每步都要有照片/串口日志/读数。

## 10. 已知阻塞与风险

1. **硬件已上机（2026-10-26 录放、2026-10-27 联网）**：本地录放链路定稿；**TURN 多轮 `result=ok`、TURN_STOP 打断 `stop_ms=20/18` 已实机验证**。打断只在出声后生效（用户 2026-10-27 定）：等待首字节期间按 BOOT 无反应，也不发 `/stop`。遗留：长回复开头的静音缺口已真机定位为「合成第二句慢于第一句音频」，切句定案（首句 ≥18、第二句 ≥20 可逗号切且 26 字上限、其余 ≥18）后真机 69.1 s 回复 0 次断流（七轮 0–5 次），**固件不用改**（1.8 s 起播水位实测无效，已回退）；每轮一次 `NetworkClient setSocketOption fail on -1 errno 9`，不影响 `status=200`。
2. **记忆只在专用启动脚本下生效**：`start-bridge.cmd` 设 `MEMORY_ENABLED=1`/`MEMORY_DRY_RUN=0`，真实目录已启用；直接运行 `server.py` 仍是关闭 + dry-run，不会积累。事实/情节只按固定句式抽取（「记住…」「我叫…」等），普通陈述只进会话摘要。`~/.voice-ai/memory/` 是明文，勿上传。
3. **定时提醒无法主动出声**：现有 HTTP 是单向请求/响应；C 已能解析并本地回复，但到点主动出声还需要新通道。不要用睡眠线程 + 直接调用 TTS 糊过去，那会绕开设备播放路径。
4. **本地命令覆盖有限**：只识别 stop/time/timer；`time` 依赖 PC 本地时钟；`timer` 只确认、不到点响（原因见第 3 条）。
5. **B 与 memory_context 重复**：B 的 `context_builder` 未接入，语音链路用 `memory_context`；不要同时接两套。
6. **测试环境分裂**：系统 Python 缺 `funasr`，`test_e2e`/`test_live` 导入失败；用 `.venv` 跑 ASR/e2e，不要把环境问题当回归。
7. **24-bit 话筒格式已由 2026-10-26 实测确定**：录音链路正常（语音从 448 ms 开始、峰值 2600–4500、underruns=0）；当前移位/位宽以 `HANDOVER-G.md` 和 `config.h` 定稿值为准，不要再按旧假设改。
8. **两路 I2S 时钟**：模块内部**没有共同时钟**（测过 7 组引脚全 open），按两路独立接；不要开 `MIC_AMP_SHARED_CLOCKS=1`（会 `#error`）。
9. **CosyVoice 慢态**：长时间运行后可能从 125–215 ms/字掉到 300–558 ms/字；重启恢复，不要换引擎或改契约。
10. **8080 入站防火墙改动属系统级**：G 只验证，不改规则；连接失败先报告。
11. **`X-Session-Id` 仍是 `d=dev1`**：`_session()` 历史 bug；记忆桶已独立解析，不影响读取，但修头值是可见契约变更，需用户确认。
12. **无硬性成本上限**：账本只是估算；没有自动停机。额度控制靠控制台。
13. **首字节约 4.4–5.2 s（真机）**：切句定案（首句 ≥18、第二句 ≥20 且 26 字上限、其余 ≥18），用 2–3 秒首字节换长回复开局无空档（真机断流 46 → 0 次）；1.5–2 s 目标降级为长期项，主因是 TTS 固定开销（真机约 0.6 s/次，E 未做）。
14. **本地兜底不是第二人格**：2B 应急模型已接入但默认关闭，只做简单问答、不写记忆；Ollama 未启动或 PC 关机时仍只能念固定句。
15. **记忆明文与个人音频**：`~/.voice-ai/memory/` 是明文短句；`bridge/out/`、`test-data/` 有真人录音。不要上传、不要贴公开仓库。
16. **git 基线已建立**：源码/文档/测试已入库（`5d7c279`、`d9cda6b` 已推送 origin/main）；`.gitignore` 已排除运行时、模型、音频、日志与 `secrets.h`。2026-10-27 三条线已提交：固件与硬件文档 `1a14b2a`、切句定案与 winperf `1205609`、本地兜底与记忆状态行 `106e57d`；**均未推送**。后续提交仍要逐项暂存，不要 `git add -A`。

## 11. 环境与路径

| 项 | 值 |
|---|---|
| 项目根 | `C:\Users\DELL\voice-ai` |
| 桥接代码 | `C:\Users\DELL\voice-ai\bridge` |
| ASR/TTS 环境 | `.venv`（Python 3.13.16）；CosyVoice worker 用 `py311`（Python 3.11.9） |
| 主服务（带记忆） | `start-bridge.cmd`（设 `MEMORY_ENABLED=1`/`MEMORY_DRY_RUN=0` 后跑 `server.py --serve`） |
| 主服务（无记忆） | `.venv\Scripts\python.exe bridge\server.py --serve` |
| 仅 edge（不占 GPU） | `.venv\Scripts\python.exe bridge\server.py --serve --no-cosyvoice` |
| 局域网 | `192.168.0.100:8080` |
| 密钥 | `~/.deepseek/key`（只确认存在，不读不打印） |
| 设备 token | `~/.deepseek/bridge_token`（不下发、不贴出） |
| 大脑配置 | `~/.voice-ai/brains.toml`（工作区外） |
| 用量账本 | `~/.voice-ai/usage.jsonl`（真实轮次后生成；无原文/密钥） |
| 记忆数据 | `~/.voice-ai/memory/`（工作区外；2026-10-23 已创建并写入） |
| 记忆开关 | `start-bridge.cmd` 设 `MEMORY_ENABLED=1`、`MEMORY_DRY_RUN=0`；其它可调 `MEMORY_DIR`、`MEMORY_INPUT_BUDGET_TOKENS=1050`、`MEMORY_CURRENT_INPUT_MAX_CHARS=500`、`MEMORY_DEBUG=1` |
| 日志 | `bridge/logs/`；音频 `bridge/out/`（个人数据，勿上传） |
| 本地插件运行时 | `D:\ollama\ollama.exe`（Ollama 0.40.8 便携版）；启动 `D:\ollama\start-ollama.cmd`，监听 `127.0.0.1:11434` |
| 本地插件模型 | `C:\Users\DELL\.ollama\models\`，`qwen3.5:2b-q4_K_M`（1.9 GB；C: NVMe 冷加载约 5.7 s；D: 旧副本已删） |
| 本地插件开关 | `LOCAL_PLUGIN_ENABLED=0`（默认）；`LOCAL_PLUGIN_MODEL`、`LOCAL_PLUGIN_MAX_TOKENS=160`、`LOCAL_PLUGIN_TIMEOUT_S=15` |
| 固件 | `C:\Users\DELL\voice-ai\firmware\`（`RECORD_ONLY` 2026-10-26 定稿；TURN / TURN_STOP 2026-10-27 上机验证） |
| 固件工具链 | `D:\arduino-cli\arduino-cli.exe` + `D:\Arduino15`（core 3.3.12），构建输出 `D:\arduino-build\` |
| 硬件 | ESP32-S3 N16R8，8 MB OPI PSRAM，16 MB flash，CH343 UART |

Windows 11，普通权限（DELL 在 Administrators 组但未提权，UAC）；Git Bash 可用；Node 24 可用；固件工具链为 arduino-cli 1.5.1（无 Arduino IDE/ESP-IDF）。

## 12. 交接报告模板

每个代码包完成后，用这个格式回报（不要贴密钥/录音）：

```text
包名：
使用的模型/思考强度：
创建的文件：
确认未修改的已有文件：
测试命令：
测试输出尾部（含通过数）：
静态检查（密钥、文件边界、契约）：
未验证/阻塞：
需要集成会话做的事：
```

G 包用检查点格式：

```text
检查点 | 操作 | 证据（照片/读数/串口） | 通过?
供电电压：
VCC-GND 是否短路：
SCK-BCLK / WS-LRCLK 导通：
COM 口：
录音回放：
HTTP 一轮：
/stop：
首字节：
当前 APP_MODE：
下一步：
```

## 13. 最终保留文件清单

### 13.1 权威源码与文档（下一步只看这些）

```text
C:\Users\DELL\voice-ai\
  HANDOFF.md                         ← 本文；总入口
  README.md                          ← 环境/模型/坑；状态以本文为准
  start-bridge.cmd                   ← 日常启动：开启记忆（MEMORY_ENABLED=1/DRY_RUN=0）
  bridge\
    README.md                        ← 契约、性能实测、记忆节
    config.py  server.py  pipeline.py  llm.py  brains.py  usage.py
    tts.py  asr_engine.py  sentence_split.py
    worker_cosyvoice.py  worker_edge.py
    memory_store.py  memory_score.py  memory_extract.py
    memory_context.py  memory_maintenance.py  memory_admin.py
    memory_writer.py                 ← 阶段 3 异步写作 + 阶段 4 维护/退出补刷（代码默认 dry-run）
    context_builder.py               ← B 归档，未接入
    commands.py  local_commands.py   ← C 已接入：解析 + 本地回复文本
    test_*.py                        ← 24 个 = 21 个 hermetic（18 unittest + 3 脚本自检）+ 3 个实跑脚本
    bench_memory_score.py            ← L3 基准
  firmware\
    README.md
    voice_terminal\
      voice_terminal.ino  config.h  audio_io.h  audio_io.cpp
      http_turn.h  http_turn.cpp  secrets.example.h
  design\
    2026-10-23-layered-memory-design.md
  handoff\
    G-hardware-bringup.md            ← 现役硬件 runbook
    B-context-memory.md  C-rule-commands.md  F-firmware-skeleton.md  ← 归档提示词
  firmware\.gitignore
```

### 13.2 诊断脚本（保留，不属于主链路）

```text
bridge\cosy_probe.py        CosyVoice 单句探测
bridge\throttle_probe.py    GPU 慢态/争用探测（占 GPU）
bridge\cosy_verify.py       CosyVoice 内容回读验证
bridge\smoke_edge.py        edge-tts 冒烟
bridge\test_edge_content.py edge 内容回读
bridge\test_e2e.py          假 LLM + 真 ASR/edge 的 HTTP 脚本（import 即启动 8088；需空 token 环境）
bridge\test_live.py         真实 DeepSeek + CosyVoice 全链（import 即花钱、占 GPU）
```

### 13.3 运行时/模型（保留，不编辑、不提交）

```text
.venv\  py311\  CosyVoice\  models\  test-data\
bridge\out\  bridge\logs\  bridge\__pycache__\
*.log  py311-embed.zip  python-3.11.9-amd64.exe  get-pip.py  matcha.tar.gz  cv.tar.gz
```

### 13.4 根目录实验/历史脚本（保留但非产品；删除前问用户）

```text
asr.py  asr_check.py  asr_test.py  asr_readable.txt  asr_result.json
tts_test.py  tts_bench.py  tts_report.json
analyze_instruct.py  instruct_test.py  instruct2_test.py  sft_instruct_probe.py
bench.log  bench32.log  clone.log  cv-deps.log  cv-deps2.log
funasr-install.log  inst.log  inst2.log  ta-install.log  torch-install.log  tts_run.log
```

### 13.5 个人数据（绝不上传）

- `bridge\out\`：真人录音与合成音频。
- `test-data\`：测试音频与探测结果。
- `~/.deepseek/key`、`~/.deepseek/bridge_token`、`~/.voice-ai/`：密钥、账本、记忆，都在工作区外。

## 14. 维护、修改、扩展怎么做

### 14.1 单一事实源

- **总状态/下一步/硬约束**：`HANDOFF.md`（本文）。
- **HTTP 契约/性能实测**：`bridge/README.md`。
- **固件接线/编译/上机确认项与遗留**：`firmware/README.md`。
- **硬件上机步骤**：`handoff/G-hardware-bringup.md`。
- **记忆设计/阶段/验收**：`design/2026-10-23-layered-memory-design.md`。
- 改数字前先重测；文档之间的旧口径以最新实测和本文为准，不要同时保留两套结论。

### 14.2 改动协议

1. **先定层**：设备/固件、HTTP 契约、bridge 对话链路、记忆、路由/账本、文档。一次只动一层。
2. **契约变更**（端点、字段、音频格式、`X-Session-Id` 值）必须先向用户确认，并同时更新 bridge、firmware、README、测试；不得只改一边。
3. **不动固件可调常量之外的代码**：上机阶段只允许改 `secrets.h` 的值和 `config.h` 里 G 手册明确列出的实测常量（`MIC_SHIFT`、`REC_SILENCE_RMS`、预缓冲/流缓冲）；改其它固件逻辑先停手报告。
4. **记忆阶段**：真实目录已于 2026-10-23 启用（`start-bridge.cmd`），改记忆或删数据前先备份 `~/.voice-ai/memory/`；删除用 `memory_admin.py`，不要手删 JSONL。
5. **默认关闭**：`config.py` 里 `MEMORY_ENABLED=0` 仍是发布默认，记忆只在 `start-bridge.cmd` 下开启；不要把默认值改成 1（会让测试/实跑脚本写入真实档案）。

### 14.3 测试门

- 任何记忆改动：记忆 **204 例**（151 纯模块 + 27 writer + 26 集成）全过；再跑脚本 27 例。读路径硬门是 **p95 ≤20 ms**（500 episodes + 500 字 + 预热 token cache）；阶段 5 实测 p50 1.93 ms / p95 2.06 ms。
- 任何 bridge 改动：显式清单 19 个 unittest 模块 **381 例**（2026-10-27 系统 Python 3.13 实测）+ 3 个脚本自检 **29 例**全过；**不要用裸 `unittest discover`**（会 import 即执行 `test_e2e.py`/`test_live.py`/`test_edge_content.py`）。`test_pipeline.py` / `test_llm.py` / `test_sentence_split.py` 是脚本式自检，**不要塞进 unittest 清单**——导入时就会跑模块级用例并改写 `llm.stream_reply` / `local_plugin.stream_reply`，污染同进程里的其他模块。
- 任何固件改动：三模式重编译，0 error；上机按 G 检查点复测。
- 不把 `funasr` 环境错误当回归；不顺手跑 `test_e2e.py`、`test_live.py` / `throttle_probe.py`。
- 提交前看 `git status`；只暂存源码/文档/测试，生成物与个人数据由 `.gitignore` 排除，不要 `git add -A`。

### 14.4 常见扩展点

| 想做的事 | 入口 | 注意 |
|---|---|---|
| 换云端大脑/加 profile | `~/.voice-ai/brains.toml` + `brain.profiles` | 密钥不进工作区；已出声不混脑 |
| 加本地插件意图 | `bridge/commands.py` + `local_plugin.py` | 只做功能，不写人格记忆；应急兜底默认关闭 |
| 加记忆类型/规则 | `memory_extract.py` / `memory_store.py` | 先加测试；敏感信息永不入库 |
| 改记忆预算 | `MEMORY_INPUT_BUDGET_TOKENS` + `memory_context` 常量 | 500 字本轮输入不静默截断；先测最坏组合；整条读路径 p95 ≤20 ms |
| 改固件模式/引脚 | `config.h` | 引脚表与契约同步；共享时钟路径未实现 |
| 换 TTS/ASR | `tts.py` / `asr_engine.py` / worker | 不改 `/turn` 音频格式；先测首字节与内容回读 |
| 加 WebSocket | 新端点 + 固件 | 先向用户确认；HTTP 保留兼容 |
| 加 VAD/打断 | 设备端 + AEC | 不能把半双工说成全双工 |
| 加唤醒词 | 固件 | 最后做；先确认 Flash/RAM 预算 |

### 14.5 版本与备份

- 已有 git 基线：`5d7c279`、`d9cda6b` 已将源码/文档/测试入库并推送到 `origin/main`；`.gitignore` 排除 `.venv`、`py311`、`models`、`CosyVoice`、`bridge/out`、`bridge/logs`、整个 `test-data/`、`*.log`、安装包与 `secrets.h`。新生成物若未被忽略，先补 `.gitignore` 再提交。
- 记忆目录、账本、密钥不进入 git。
- 升级前备份 `~/.voice-ai/brains.toml`、`~/.voice-ai/memory/`、`~/.voice-ai/usage.jsonl`（如存在）。

### 14.6 GPU 与并发

- 跑 CosyVoice / `test_live.py` / `throttle_probe.py` 前先确认没有别的会话占卡；不要用 CUDA 探测打扰别人。
- CosyVoice 慢态先重启恢复；不要换引擎掩盖。
- E 预热只能串行做，不能与其他 GPU 任务并行。

## 15. 参考

- 契约与实测：`bridge/README.md`
- 环境与模型：`README.md`
- 工程日志：`bridge/工程日志-2026-10-20.md`
- 记忆设计：`design/2026-10-23-layered-memory-design.md`
- 硬件 runbook：`handoff/G-hardware-bringup.md`
- 四份外包提示词：`handoff/` 目录（B/C/F 归档，G 现役）
