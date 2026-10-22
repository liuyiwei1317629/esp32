# ESP32 语音陪伴 · PC 端五层记忆设计

日期：2026-10-23  
状态：阶段 1/2/3 已验收；阶段 4 代码接线已完成（2026-10-23：维护、读路径 debug、退出补刷），默认仍为 `MEMORY_ENABLED=0` + writer dry-run（`MEMORY_DRY_RUN=1`）；真实 `~/.voice-ai/memory/` 写入待用户确认  
范围：`C:\Users\DELL\voice-ai\bridge`，记忆数据放工作区外 `~/.voice-ai/memory/`

## 0. 结论

采用五层记忆，但全部放在 **PC bridge**：

- 设备仍是“身体”，只负责录音、播放、按键；不存人格、不存记忆、不参与抽取。
- 云端仍是唯一的人格/对话大脑；PC 端的记忆库是云端大脑的**外部记忆**，本地 Qwen 插件无权读写。
- `/turn` 请求、`/stop` 语义、WAV/PCM 音频契约零改动；固件零改动；记忆本身不需要新端点。
- **已知契约问题（待 bb96）**：现有 `server.py:87-92` 的 `pair[9:64]` 对 `session_id=dev1` 返回 `d=dev1`，响应头 `X-Session-Id` 也是这个错误值。修正它属于可见契约变更，单列为待确认项；记忆实现使用独立解析器，不依赖该响应头。
- 默认输入预算 **1050 token/轮**：本轮输入恢复到现有 `context_builder` 的 500 字上限，历史 2×40/50，L0–L3 共 350，另留 20 给 header。650 只够 120 字输入，不再作为默认。
- 读路径同步、毫秒级；写路径与巩固异步，不进首字节路径。
- 先规则抽取、规则摘要；LLM 抽取/摘要作为可选增强，单独记账、可关闭。

非目标：

- 不做设备端离线记忆（PC/网络不在时设备本来就不能对话）。
- 不做主动提醒；待办只能作为对话记忆，不能承诺“到点叫你”。
- 不实现语音“忘掉”命令（C 包 v1 明确不扩 `kind`）；删除走本地 CLI。
- 不自动合并 DSH / mnemon 的记忆。

## 1. 与现有约束的对齐

| 约束 | 本设计如何满足 |
|---|---|
| 云端唯一负责人格/长期记忆 | 记忆库由 bridge 在云端回合后维护，本地插件只读命令、不读写记忆 |
| 不把完整会话/密钥写进工作区 | 记忆目录在 `~/.voice-ai/memory/`，只存短事实/摘要/最近两轮短句，不存音频和完整对话 |
| 设备只持有 bridge_token | 设备不新增任何记忆存储或接口 |
| 固件改契约先报 bb96 | 本设计不新增端点/字段；但现有 `X-Session-Id` 值因 `_session()` 解析 bug 为 `d=dev1`，修正它是可见契约变更，需 bb96 确认；记忆桶不依赖该头 |
| 用户说“停”立即停 | 被打断/未完成的轮次不写入记忆；已完成并已入队的回合不受后续 `/stop` 或新 gen 影响 |
| 现有 A/D/B/C 边界 | 不改 A/D/B/C 交付文件；新增记忆模块；集成只改 `config.py`/`pipeline.py`/`server.py`，需逐项授权 |

## 2. 总体架构

```
ESP32 ──WAV──► server.py
                 │
                 ├─ ASR ─► user_text
                 │
                 ├─ MemoryReader ─► L1/L2/L3 + 最近2轮 + 本轮
                 │                     │
                 │                     └─ memory_context 组装 messages
                 │
                 ├─ pipeline.Turn ─► LLM 流式 ─► 切句 ─► TTS ─► PCM
                 │                                              │
                 └─ 流正常完成（completed） ─► MemoryWriter（异步队列）◄─┘
                                          │
                                          ├─ 规则抽取
                                          ├─ 可选 LLM 抽取/摘要
                                          ├─ 去重/冲突/revision
                                          └─ facts/episodes/sessions 落盘
```

组件（全部新建，除集成点外不改已有文件）：

| 文件 | 职责 |
|---|---|
| `bridge/memory_store.py` | 记录模型、JSONL 读写、原子替换、进程内锁 + 跨进程锁、ID、去重、revision/tombstone、mtime 变化重载 |
| `bridge/memory_extract.py` | 规则抽取、敏感信息过滤、可选 LLM 抽取入口 |
| `bridge/memory_score.py` | 分词、相似度、时间衰减、待办加权、top-k |
| `bridge/memory_context.py` | 五层组装与总预算硬裁；复用 `context_builder` 的公开 `clip_text`/`trim_history`/`appended_history`/`estimate_tokens`；历史长度按本节 40/50 显式裁剪，不依赖其默认 120/120 |
| `bridge/memory_writer.py` | 单线程异步写队列；回合结束后抽取/摘要/落盘 |
| `bridge/memory_maintenance.py` | 晋升/降级/tombstone/压缩；启动与定时触发 |
| `bridge/memory_admin.py` | 本地 CLI：list/pin/unpin/delete/export/stats/why |
| `bridge/memory_llm.py` | 可选：复用 OpenAI 兼容 profile 做摘要/抽取，独立记账 |
| `bridge/test_memory_*.py` | 纯标准库单测；不联网、不占 GPU |
| `bridge/bench_memory_score.py` | 500 条 episode 的检索基准；只打印 p50/p95，不接主链路 |

## 2.1 会话 ID 与 `X-Session-Id`（待 bb96）

现状已核对 `server.py:87-92`：

```python
if pair.startswith("session_id="):
    return pair[9:64]  # "session_id=dev1" -> "d=dev1"
```

- 这个值同时用于流式回合和响应头；`X-Session-Id` 当前实际值就是 `d=dev1`。
- 修正它会改变响应头可见值，属于契约变更；**未获 bb96 确认前不改 `server.py`**。

记忆模块使用独立解析器，不复用 `_session()`，也不读 `X-Session-Id`：

```python
import urllib.parse

def parse_session_id(query):
    for pair in query.split("&"):
        if pair.startswith("session_id="):
            raw = urllib.parse.unquote(pair.split("=", 1)[1])
            sid = raw[:55]
            return sid or "anon"
    return "anon"
```

- 顺序固定：按第一个 `=` 分离 → URL decode → 截 55 字符；空值/缺失才用 `anon`。
- 不做字符白名单，不把含 `/`、`.`、`%` 等字符的 ID 并到 `anon`；文件名用 `sha1(sid)[:16]`，`../`、绝对路径、超长值都不能越界。
- 55 字符截断后不同原值可能落到同一个桶；这是明确接受的设备 ID 风险，桶内记录仍带截断后的 `session_id`。
- `X-Session-Id` 修复方案：`_session()` 改成同样的分离 + decode + 截 55，响应头从 `d=dev1` 变为 `dev1`；其余响应头、状态码、chunked 行为不变。**待 bb96 确认后才能改。**
- 无论 header 是否修，记忆桶都按 `parse_session_id()` 的结果算；不得依赖 `X-Session-Id` 的当前错误值。

## 3. 五层与预算

单位：中文按 1 字≈1 token，最终用 `context_builder.estimate_tokens()` 校验；预算按**最终 messages**计，不按原始字段。

| 层 | 内容 | 注入上限 | 读取 |
|---|---|---:|---|
| L0 人格 | `config.DEFAULT_PERSONA` | 120 | 每轮固定 |
| L1 核心事实 | 姓名、关系、长期偏好（pinned 优先） | 60（2 条 × 30 字） | 每轮固定 top-2 |
| L2 会话摘要 | 摘要 + 一行“当前目标” sticky | 80（存储 summary+sticky ≤100；summary 无 sticky 时 ≤96；sticky ≤20） | 每轮固定 |
| L3 相关往事 | 事件、近况、待办 | 90（top-3 × 30 字） | 相似度 + 时间衰减 + 待办到期 |
| L4 工作记忆 | 最近 2 轮 + 本轮输入 | 680（500 + 2×90） | 原样，超限只砍历史 |
| 层小计 | | **1030** | |
| header/分隔符预留 | `长期记忆：`/`之前聊到：` 等 | 20 | |
| 硬上限 | | **1050** | |

1050 是**最终 messages 的硬上限**；表中的层预算是分配目标，header 和分隔符也算在总盘内。超限时按下面的硬裁顺序让位，不允许静默把本轮输入截到 120。

L4 明细：

- 本轮输入 ≤500 字，与 `context_builder.CURRENT_INPUT_MAX_CHARS` 一致。
- 历史每轮：user ≤40 字、assistant ≤50 字；最多 2 轮。
- 500 + 2×(40+50) = 680。

为什么是 1050：

- 此前的 530 方案连两轮历史（180）加正常输入都放不下；650 也只能配 120 字输入。用户已确认放宽，本轮输入恢复到现有 500 字上限，避免正常语音被静默截断。
- 120（L0）+ 60（L1）+ 80（L2）+ 90（L3）+ 680（L4）= 1030，再留 20 给 header/分隔符，硬上限 1050。
- 成本：按 `deepseek-flash` 高峰价（输入 ¥2/M、输出 ¥8/M），1050 输入 + 典型 50 输出 ≈ 1100 token/轮 ≈ **¥0.0025/轮**，100 轮/天约 **¥0.25/天**；若输出打满 320，约 **¥0.0047/轮**。未计缓存命中折扣，D 账本会体现。
- 可配置：`MEMORY_INPUT_BUDGET_TOKENS=1050`。若主动下调到 650，必须同时把本轮输入上限显式降到 120，并在 debug 标 `low-budget`，不能悄悄回退。

硬裁顺序（最终 messages 超 1050 时）：

1. L2：整体 80 → 60 → 40；先丢旧摘要，再压回复首句尾部，sticky 最后；若 40 仍不足，保留 sticky 与最新意图的最短前缀（合计 ≤40），允许丢弃回复首句。
2. L3：丢掉分数最低的一条，90 → 60 → 30。
3. L4：只砍历史——丢最旧的一轮（90），再把 assistant 50 → 30、user 40 → 20；**本轮输入 500 不动**。
4. L1：只保留分数最高的 1 条（60 → 30）。
5. L0 与本轮输入不砍；若仍超 1050，继续裁 L2/L3 到 0 并告警。初始 1050 = 500 输入 + 180 历史 + 120 persona + 60 L1 + 80 L2 + 90 L3 + 20 header。

任何一步都要重新 `estimate_tokens`，不允许“按字符数估个大概”就放行。

## 4. 存储 schema

目录：

```
~/.voice-ai/memory/
  facts.jsonl          # L1 事实（含历史值与 tombstone）
  episodes.jsonl       # L3 事件/近况/待办
  sessions/
    <sha1(session_id)[:16]>.json  # L2 摘要 + sticky + 最近 2 轮
  state.json           # schema_version / last_maintenance / counters / at_capacity
  state.lock           # 跨进程写锁（bridge 与 CLI 互斥）
  memory_usage.jsonl   # 可选 LLM 调用账本（仅 token/费用，无原文）
  memory_debug.jsonl   # 可选：只记命中 id/分数/预算，不记原文
```

`facts.jsonl` 每行一个完整记录（不是事件流）：

```json
{
  "id": "f3a1c2d4e5f60718",
  "kind": "name",
  "text": "用户叫小林",
  "slot": "name",
  "pinned": true,
  "status": "active",
  "tombstone_reason": null,
  "revision": 2,
  "history": [{"text": "用户叫小明", "revision": 1, "retired_at": "2026-10-20T10:00:00+00:00"}],
  "created_at": "2026-10-20T09:00:00+00:00",
  "updated_at": "2026-10-23T01:00:00+00:00",
  "last_used_at": "2026-10-23T01:00:00+00:00",
  "hits": 4,
  "source": "explicit"
}
```

`episodes.jsonl`：

```json
{
  "id": "e9b8c7d6a5f40312",
  "text": "用户下周要交设计稿",
  "kind": "todo",
  "slot": "todo:design-review",
  "status": "active",
  "created_at": "2026-10-23T01:00:00+00:00",
  "event_at": null,
  "due_at": "2026-10-30T00:00:00+00:00",
  "last_used_at": null,
  "hits": 0,
  "source": "rule"
}
```

`sessions/<sha1(session_id)[:16]>.json`：

```json
{
  "session_id": "dev1",
  "summary": "最近工作有点累；今晚早点休息；之前聊了工作压力和周末安排。",
  "sticky_goal": "下周交设计稿",
  "sticky_todo_id": "e9b8c7d6a5f40312",
  "recent_turns": [
    {"user": "最近工作有点累", "assistant": "那今晚早点休息呀", "ts": "2026-10-23T00:58:00+00:00"}
  ],
  "updated_at": "2026-10-23T01:00:00+00:00",
  "completed_turns": 12
}
```

- `recent_turns` 最多 2 条，写入时就裁到 user ≤40 字、assistant ≤50 字；这是唯一保留用户原话的地方，不存完整对话。
- `summary` 本体上限 96 字（sticky 为 0 时可达）；`sticky_goal` ≤20 字；`summary + sticky_goal` 合计 ≤100 字（100 的额度包含 sticky）。超出时先裁旧摘要 38→20→0，sticky 最后压。
- 会话文件名用 `sha1(session_id)[:16]`；`session_id` 按 §2.1 解析，任意 ≤55 字符都接受，空/缺失才用 `anon`；不做字符白名单、不拼路径，也不使用 `X-Session-Id` 头值。

规则：

- **ID**：创建时 `sha1(kind + ":" + normalized_text)[:16]`；normalized = NFKC + 去首尾空白 + 去标点。写入后 ID 固定，slot 冲突改 text 时不重新算 ID（保持引用稳定）。
- **kind 枚举**：`fact | event | near_term | todo | preference | relation | residence | name`；未知 kind 读取时忽略并计数。
- **去重**：同 ID 只更新文本/槽位相关字段，**提及不增加** `hits`/`last_used_at`；这两个字段只在被 L1/L3 注入时由 `mark_used` 更新。同 ID 被显式“记住”时把 `pinned=true`、`source="explicit"`。同 slot 不同值 → 沿用该 slot 现有记录的 `id`，替换 `text`、`revision+1`，旧值进 `history`，不新建第二条、不物理删。
- **删除**：`status="tombstone"`，读取时过滤；`purge` 才物理删。tombstone 记录 `tombstone_reason`（`deleted|demoted|promoted|expired|capacity`）。
- **复活**：显式“记住X”命中 tombstone（同 ID 或同 slot）→ `status="active"`、`revision+1`、旧值进 `history`、`pinned=true`、`source="explicit"`、`tombstone_reason=null`；普通规则抽取和 LLM 抽取**不复活** tombstone，避免已删内容自己回来。
- **时间**：所有 `ts/created_at/updated_at/last_used_at` 存 UTC 带偏移的 ISO 8601；`due_at` 按本地时区解析后也转 UTC，维护任务统一用 aware datetime 比较。
- **原子写**：写 `.tmp` → `flush` + `fsync` → `os.replace`；保留上一版 `.bak`。
- **进程内锁**：每个文件一个 `threading.RLock`；单进程多 HTTP 线程安全。
- **跨进程锁**：bridge 与 `memory_admin.py` 共用一个 `state.lock`（`O_CREAT|O_EXCL`，超时 2 s）；拿不到锁的写操作放弃并计数，CLI 直接报错退出。锁文件写入 pid + 获取时间，超过 10 s 视为崩溃遗留，可抢占并告警。锁只覆盖“读-改-写”的几毫秒，不覆盖整个回合。
- **外部修改重载**：读路径每次组装前对 facts/episodes/session 文件做 `stat`（每轮 3 次，目标 <1 ms、未单独实测）；mtime 或大小变了就重载该文件，保证 CLI 删除/置顶后 bridge 不用重启。
- **schema 版本**：`state.json` 存 `schema_version=1`；遇到更高版本 → 只读模式 + 启动告警，拒绝写入，绝不猜测迁移。
- **损坏行**：JSONL 逐行解析，坏行跳过并计数；若整个文件 0 条可解析且 `.bak` 非空，先用 `.bak` 恢复（坏文件另存 `.corrupt`）。
- **上限**：active facts ≤200，active episodes ≤500。淘汰只针对 **unpinned** facts（按 `last_used_at` 最旧 + 低 hits）；pinned 永不被自动淘汰、不被自动降级。
- **pinned 超限**：若 unpinned 淘汰完仍超 200（即 pinned 本身超限），`state.json` 置 `at_capacity=true`，暂停自动抽取/晋升并告警；显式“记住”仍可写入且 pin，但会计数并提示用 CLI 整理。绝不为凑上限静默删除 pinned。
- **pinned 注入**：L1 仍只注入 top-2；若 pinned >2，按 `last_used_at/hits` 选 2 条，其余留在库里，`stats`/debug 显示 `pinned_overflow`，不自动取消 pin。
- **不存**：音频、完整 ASR 文本、完整回复、prompt 快照、密钥/token、身份证/银行卡/密码。

## 5. 读路径

每轮 ASR 后、调用 LLM 前：

1. 取内存快照：`facts`（active + pinned）、`sessions/<hash>`（按 §2.1 解析后的 session）、`episodes`（active）、`recent_cache`（按 session 分桶）。读路径把 `recent_cache` 与磁盘 `session.recent_turns` 按 `ts + user + assistant` 去重合并，取最近 2 条；快照常驻内存，文件 mtime 变了才重载，不每轮全量读盘。
2. L1：排序键 `(pinned 降序, last_used_at 降序, hits 降序)`，取 top-2；`last_used_at` 为空按最旧。pinned >2 时只注入 top-2，其余不注入并告警（见 §4）。
3. L2：按 §6 的“最新在前”摘要注入；正常路径已构造到 ≤80，`clip_text(…, 80)` 只作最后保险，截尾时先掉最旧的摘要，最新句与 sticky 保留。
4. L3 打分：
   ```
   sim   = overlap(q, e) / max(1, min(|q|, |e|))
   rec   = exp(-Δt / τ)          # 事件 τ=7d，偏好 τ=30d
   visit = min(0.5, 0.1 * hits)
   score = 0.6*sim + 0.3*rec + 0.1*visit
   ```
   - 待办：`rec` 换成 `due_score`。`due_at` 已过或 24 h 内 = 1.0；超过 24 h 后按 `max(0, 1 - 剩余天数/7)` 线性降到 0；无截止日 = 0.3。待办不因“旧”被压掉，但也不承诺提醒。
   - `Δt` 基准：事件取 `event_at or created_at`，偏好取 `updated_at`；缺失时取 `created_at`。
   - 过滤：`sim > 0` 才进候选；24 h 内到期的待办可豁免。
   - top-3；同分时越新越优先（与 B 一致）。
   - `sim` 用与 B 相同的中文 bigram + 英文/数字词切分；`memory_score` 自带公开 tokenizer，不调用 B 的私有 `_tokens`。
   - **token 缓存**：每条 episode 的 token 集在加载、新增、改文时算一次并缓存在内存；每轮只 tokenize 查询，500 条不逐轮重算。mtime 重载或改文时使对应缓存失效。
5. 更新命中：被注入的 L1/L3 记录调用 `store.mark_used(facts=..., episodes=..., persist=False)`，在内存里 `hits += 1`、`last_used_at = now`；未注入的记录不计数，**提及也不计数**。**读路径不写盘**。阶段 2 只读接入不调用 `mark_used`。阶段 3 已实现：**真实写入模式**（`MEMORY_DRY_RUN=0`）下，纯命中每 5 个完整回合批量刷一次、进程退出补刷，每次只在最后一条 `mark_used` 时落盘；**dry-run 只累计 `hits_deferred`，不调用 `mark_used`**。刷新失败不影响回复，只记 `hit_errors`。
6. 组装：`memory_context.build_messages()` 按第 3 节预算拼接，格式与 `context_builder.build_messages()` 一致：
   ```
   system: persona
           长期记忆：
           - ...
           之前聊到：...
   user/assistant: 最近 2 轮（来自 sessions/<hash>.json 的 recent_turns）
   user: 本轮输入
   ```
   L4 历史只从 `recent_turns` 与 `recent_cache` 的合并结果取；没有历史就是空，不回查其他会话。
   ```python
   build_messages(persona, facts, summary, sticky_goal, episodes,
                  recent_turns, user_text, budget_tokens=1050)
   # 返回 (messages, info)；info 含各层条数/裁剪记录；selected_ids 供写路径计命中，其余用于 debug
   ```
7. 任何一步异常 → 回退到“persona + 本轮输入”两条消息，不影响主链路。

读路径延迟：**L3 打分目标 <5 ms**。2026-10-23 基准（500 条 episode × 200 次查询，L3 打分 + token 缓存，Python 3.13/Windows）：**p50 1.26–1.36 ms、p95 1.42–1.64 ms、max ≤2.42 ms**（seed 7/8/9）。整条 `build_for_turn` 已补测（store 读取 + L3 + 组装）：独立复核 p50 6.1–6.4 ms / p95 6.6–10.7 ms，本机高相似数据 p50 11.5–13.0 ms / p95 13.3–15.0 ms；主因是 `active_episodes()` 深拷贝 500 条。**整条路径硬门为 p95 ≤20 ms**，<5 ms 保留为优化目标，详见 §14 第 10 条与 §15。**阶段 5（2026-10-23）已改为只读投影：本机 500 条复测 `active_episodes` p50 0.10 ms（原 3.90 ms），`build_for_turn` p50 1.93 ms / p95 2.06 ms（原 5.72 / 6.22 ms），见 §20。**

## 6. 写路径

触发条件（入队时生成不可变 `WriteJob` 快照，worker 只看快照，不再看活的 `turn`/`bridge.gen`）：

- `completed=True`（`pcm_chunks` 正常耗尽，不是 stale/abort）。**`completed` 优先级最高：一旦入队快照里为真，就不因随后开始的新 gen、`/stop` 或 `abort` 置位而失效。**
- 快照里 `error is None`。
- **`stats["done"] is True` 或 `stats["finish_reason"] == "stop"`**；两者都没有时拒绝。clean EOF 但没有 `[DONE]`、也没有 `finish_reason=stop`，视为可能半截回复，不写。
- `finish_reason != "length"`（即使 `done=True`，被 max_tokens 截断也不算完整回合）。
- ASR 文本与回复文本都非空。
- `aborted` 只作为诊断字段写进快照和日志；对 `completed=True` 的快照不参与拒绝判断。`completed=False` 的回合不生成快照（客户端在完成前断开也不会置 `completed`）。

流程：

```
server 流结束（生成 WriteJob 快照）
  → 同步敏感预检（命中 → 不缓存、不入队、计数、结束）
  → memory_writer.enqueue(job)
       # 同步、非阻塞：先把裁剪后的 (user, assistant, gen, ts) 写入 recent_cache
       # 下一轮读路径立即可见，不等待 worker 落盘
  → 单个 daemon worker 串行处理
      → 敏感信息预检（第二次，防御性；命中 → 整轮不写、计数、结束）
      → 规则抽取（高置信）
      → 去重 / slot 冲突 / revision / tombstone 复活
      → 更新 L2 摘要 + 合并 recent_cache 到 recent_turns
      → 核心落盘（facts/episodes/session）      ← 先于可选 LLM
      → 可选 LLM 增强（默认关闭）
          → 校验 / 二次敏感过滤
          → 只有新增内容才二次落盘
```
`assistant_text` 由 `Turn.reply_text` 提供；`server` 不再单独维护一份。

可选 LLM 增强在核心落盘后执行；开始前若发现已有新 gen 在跑，直接跳过增强（核心已安全），避免与下一轮 ASR/TTS 抢 GPU。若 active profile 是本地模型，这种抢占尤其明显，默认关闭。

规则抽取（v1）：

| 类型 | 句式（去标点后） | 槽位/动作 |
|---|---|---|
| 显式记忆 | `记住X`、`记一下X`、`帮我记住X`、`别忘了X` | fact，`source=explicit`，可直接 pin |
| 姓名 | `我叫X`、`我的名字是X`、`你可以叫我X` | slot=`name` |
| 偏好 | `我喜欢X`、`我不喜欢X`、`我最喜欢X`、`我讨厌X` | slot=`pref:<对象>`，带 polarity |
| 居住/工作 | `我住在X`、`我在X工作`、`我在X上学` | slot=`city`/`work`/`school` |
| 关系/宠物 | `我养了X`、`我的(爸爸/妈妈/…)(是/叫)X` | slot=`pet:<名>`/`relation:<关系>` |
| 待办 | `我(今天/明天/后天/下周…)(要/得/需要)X`、`提醒我X` | kind=`todo`，能解析到期日就填 `due_at` |

存储路由：显式“记住” → `facts` 并 pin；姓名/居住/关系 → `facts`（不 pin）；偏好与待办 → `episodes`（偏好被注入 3 次后再晋升 `facts`）；v1 不写一般事件。这样长尾偏好不会被“L1 只取 top-2”卡死，仍能在 L3 被检索到。

约束：

- 只存抽取出的短句（≤30 字），不存整段对话。
- 短句必须包含实际内容，不能只是“喜欢”“明天要”。
- ASR 口头词（嗯/呃/那个）先去掉；疑问句、假设句（如果…）、第三人称转述默认不抽。
- 否定偏好要保留极性（“不喜欢香菜”不能写成“喜欢香菜”）。
- **敏感信息永不自动入库**：server 在更新 `recent_cache` 前先做一次同步预检；命中身份证、银行卡、手机号、密码、验证码、API key/token、私钥、精确住址门牌等模式 → 本轮不缓存、不入队、不抽取、不调用 LLM、不更新摘要/`recent_turns`，只计数。worker 开头再做一次相同预检（防御性）；逐条抽取结果再过一次同一过滤器。`recent_cache` 与 `recent_turns` 都不得出现敏感值。
- 待办只作为对话记忆；回复里不得说“到点我会提醒你”。
- 到期日解析以回合发生时的**本地时区**为锚：`今天/明天/后天` = 当日 + N 天 00:00；`下周X` = 下周一 + 星期偏移 00:00；解析不了就不填 `due_at`（按无截止日 0.3 分），不猜。
- **v1 规则只写 todo 与明确的事实类条目**；一般事件/近况不自动抽（噪声大），只进 L2 摘要。阶段 5 开启 LLM 后才可选补 `event`/`near_term`，仍受置信度与敏感过滤约束。

可选 LLM 抽取：

- 仅在规则没有抽到候选、且本轮文本包含“记住/喜欢/住在/要/计划”等线索时触发。
- 使用当前 active profile 的副本（`dataclasses.replace(profile, max_tokens=160, first_token_timeout_s=3.0, timeout_s=min(profile.timeout_s, 20.0))`），直接走 `llm.stream_profile`，**不经过 BrainRouter**，避免后台失败污染对话脑的冷却/健康状态；另用 20 s 的 abort 事件兜底。
- 发送内容只有：本轮 user 的前 120 字（即使本轮输入上限是 500 字）+ 回复首句（≤50 字）+ 现有 L1 短句；不发音频、不发历史全文。
- 要求输出 JSON 数组，逐条校验 `kind/text/slot/confidence`；只接受 `confidence >= 0.7`，再过一次敏感过滤。
- 限频：最多每 4 个完整回合 1 次；失败静默跳过。
- 记账到 `memory_usage.jsonl`（`profile/prompt_tokens/completion_tokens/cost_cny/ts/purpose`），不进 D 的 `usage.jsonl`，避免改 D 的 schema。

L2 摘要：

- **阶段 1（默认，规则）**：
  - `sticky_goal`：本轮抽到 todo/目标句就替换，并记下 `sticky_todo_id`；对应的 todo 被 CLI 删除（tombstone）时清空；没有新目标就保留。`sticky_goal` ≤20 字。v1 没有 `done` 命令，待办完成由用户删除表达。
  - 存储顺序**最新在前**：`summary = 本轮意图；回复首句；上一版摘要`。构建上限：本轮意图 ≤28、回复首句 ≤28、上一版摘要 ≤38，两处分隔符后 `summary` 本体 ≤96；`summary + sticky_goal` 合计 ≤100（sticky 占用这 100 的额度）。超出时先裁旧摘要 **38→20→0**，仍超再压回复 28→18、意图 28→18；sticky 最后压（20→16），绝不裁掉本轮最新句。
  - 注入串：`sticky_goal｜本轮意图｜回复首句｜旧摘要`。正常上限为 sticky 20 + 意图 28 + 回复 28 + 3 个单字分隔符 = 79 ≤80，因此 `clip_text(…, 80)` 只会先截掉尾部旧摘要，够不到最新两句；旧摘要已经为 0 时若仍超，才压 sticky 20→16（sticky 最后）。旧摘要主要靠 L3 接续，L2 的 80 字优先保证最新句。
  - 测试：**96 字旧摘要 + 新回合**，注入必须包含最新句；旧摘要允许被裁到 0。
  - “本轮用户意图” = 去口头词后的 ASR 文本前 28 字；“回复首句” = 回复第一个句号/问号/感叹号前的文本，最多 28 字；摘要不写敏感值。
- **阶段 2（可选，LLM）**：每 6 个完整回合，用“上一版摘要 + `recent_turns`（≤2 轮）+ 本轮”生成 summary+sticky 合计 ≤100 字（其中 sticky ≤20）的摘要；失败回退规则摘要。独立记账、独立限频。v1 不做“空闲 10 分钟”触发，避免多一个定时器。
- 摘要生成永远在回合结束后异步执行，不阻塞音频。

写队列：

- `queue.Queue(maxsize=64)`；单个 daemon worker。
- `recent_cache`：进程内线程安全，按 session 分桶，每个 session 最多 2 条；`enqueue()` 在入队前同步更新，下一轮读路径立即可见；worker 落盘后用磁盘 `recent_turns` 对齐，二者按 `ts + user + assistant` 去重。
- 队列满 → 丢弃新任务并计数告警，宁可少记，不可拖慢回复。
- 同一 `gen` 只处理一次；资格在入队时快照，**已入队的 completed job 即使随后开始新 gen 也照常落盘**；未完成的 gen 不入队。
- worker 异常只写日志，不影响主链路；服务退出时最多等待 2 s。

## 7. 巩固与维护

启动时 + 每 24 h + 每 50 个完整回合触发一次（后台线程，持锁，毫秒级）：

- **晋升 L3 → L1（v1）**：同一条 active `preference` episode 被 L3 **选中注入**累计 `hits >= 3` → 生成 fact（`source="promoted"`，默认不 pin），原 episode 置 tombstone（`promoted`），避免同一内容在 L1/L3 重复注入。relation/residence/name 已在 §6 直接写 `facts`，v1 不存在这些 episode，不设晋升分支；`todo` 不晋升。只是被加载、没被选中的不计 hits。若目标 fact 仍是 tombstone（例如同 slot 的 fact 已 `demoted`），`upsert_fact` 不会复活，此时**保留 episode、不增加 promoted**；是否允许维护复活 `demoted` fact 留待单独决定（`deleted` 永不自动复活）。
- **显式晋升**：用户说“记住X” → 直接 fact，`pinned=true`，`source=explicit`。
- **降级 L1 → L3**：未 pinned 的 fact，`last_used_at` 超过 30 天 → 先建 episode（`source="demoted"`，`fact_id` 指向原 fact），再把 fact 置 tombstone（`tombstone_reason="demoted"`）；超过 180 天直接 tombstone（`expired`）。pinned 永不被自动淘汰、不被自动降级；pinned 自身超限时按 §4 进入 `at_capacity`。
- **episode 清理**：180 天无命中 → tombstone；tombstone 满 1 年可物理清理。
- **合并**：normalized text 相同 → 合并 hits；同 slot 冲突 → revision+1，旧值进 history。
- **容量**：episodes >500 淘汰最弱 active；facts >200 只淘汰 unpinned（pinned 不参与自动淘汰）；pinned 自身超限时按 §4 进入 `at_capacity`，暂停自动抽取/晋升并告警。
- **幂等**：同一维护任务重复跑不产生重复记录；`state.json` 记录 `last_maintenance`。维护判断晋升前先合并内存中尚未刷盘的 `hits`（或先触发一次命中刷盘），避免刚满 3 次注入的偏好因计数未落盘而漏晋升。

## 8. 集成点（实现时需要授权）

只改三个已有文件，另有一处 `memory_context.py` 小改；都是小改：

`config.py`：

```python
MEMORY_ENABLED = os.environ.get("MEMORY_ENABLED", "0") == "1"
MEMORY_DIR = os.environ.get(
    "MEMORY_DIR", os.path.join(os.path.expanduser("~"), ".voice-ai", "memory")
)
MEMORY_INPUT_BUDGET_TOKENS = _env_int("MEMORY_INPUT_BUDGET_TOKENS", 1050)
MEMORY_CURRENT_INPUT_MAX_CHARS = _env_int("MEMORY_CURRENT_INPUT_MAX_CHARS", 500)
MEMORY_DRY_RUN = os.environ.get("MEMORY_DRY_RUN", "1") == "1"
MEMORY_DEBUG = os.environ.get("MEMORY_DEBUG", "0") == "1"
# 计划中，尚未加入：MEMORY_LLM
```

- 阶段 2 落地前 4 项；**阶段 3 已补 `MEMORY_DRY_RUN=1`（默认）与 `MEMORY_DEBUG=0`**。`MEMORY_LLM` 仍属于阶段 5，尚未加入。`store.read_only`（`schema_version` 过新）时 writer 被强制 dry-run，无视 `MEMORY_DRY_RUN=0`。
- 数值环境变量统一走 `_env_int`/`_env_float`：非法值打印 `WARN ... using default` 并回退，不在 import 时崩溃。`state.json` 的 `schema_version` 高于支持值时，`build_bridge` 启动打印 `WARN ... read-only`，store 进入只读。
- 默认关闭是刻意的：阶段 2 起显式 `MEMORY_ENABLED=1` 才读；阶段 4 验收通过后再讨论是否改默认。
- `MEMORY_DRY_RUN=1` 时读路径照常，`enqueue` 照常同步更新 `recent_cache`（下一轮可见）并做敏感预检；worker 照常抽取/组装并统计 `dry_run_jobs`/`dry_run_candidates`/`dry_run_summary_chars`，但**不落 facts/episodes/sessions、不调 `mark_used`**，命中只记 `hits_deferred`。
- 本轮输入上限默认 500；下调总预算时它不会自动改，必须显式设 `MEMORY_CURRENT_INPUT_MAX_CHARS`，不存在悄悄回到 120 的路径。
- 各层条数/字数上限是 `memory_context.py` 的模块常量，不全部做成环境变量；只有总预算、本轮输入上限、开关、目录可配。
- `MEMORY_DIR` 只在启用后才创建。

`pipeline.py`：

- `Bridge.begin(user_text, timers=None, session_id="anon")`，默认值兼容旧调用。
- `Turn` 增加 `session_id`、`reply_text`、`completed`，并保留 `stats["done"]`、`stats["finish_reason"]`。
- `on_content` 累积 `reply_text`（stale 时忽略）。
- `pcm_chunks` 正常结束时置 `completed=True`；abort、stale 或被新回合取代（`bridge.gen != turn.gen`）时不置。该判断只用于“尚未置位”的回合；`completed` 一旦置位，不因后续新 gen、`/stop` 或 abort 回退。
- server 入队时从 `Turn` 生成不可变 `WriteJob`（session/completed/error/aborted/done/finish_reason/gen/reply/user/ts）；worker 只认这份快照，不再读活的 `turn` 或 `bridge.gen`。
- 消息组装改为 `memory_context.build_for_turn(persona, session_id, user_text)`（内部完成读路径和预算裁剪）；记忆关闭或异常时回退旧的 system+user 两条。
- 维护触发：`MemoryMaintenance.maybe_run(completed_turns=...)` 的 `completed_turns` 是**距上次维护的增量**，不是会话累计；调用方按上次维护后新增的完成回合数传入，传累计值会在第 50 回合后每轮触发维护。
- `reply_text` 不落盘原文；writer 只取首句/要点进摘要。

`server.py`：

- 现有 `_session()` 的 `d=dev1` bug 先不动；记忆接入用 §2.1 的独立 `parse_session_id(query)` 得到正确 session 再传给 `bridge.begin`，**不读也不依赖 `X-Session-Id` 响应头**。
- `X-Session-Id` 修复单列：获 bb96 确认后把 `_session()` 改为同样的分离 + decode + 截 55，响应头由 `d=dev1` 变为 `dev1`；未确认前保持现状。该修复不得影响记忆桶。
- 流正常结束时，先在 `pcm_chunks` 正常耗尽、`completed=True` 的同一时刻同步生成 §6 的 `WriteJob` 快照，并调用 `memory_writer.enqueue(job)`：`enqueue` 先做同步敏感预检，再更新 `recent_cache`，最后非阻塞入队；**然后**才发送终块 `self._chunk(b"")`。不能把 cache 更新留到终块发送之后：下一个 `/turn` 可能紧接终块到达，必须已能在 `recent_cache` 看到上一轮。
- 若终块发送失败或客户端随即断开，已完成快照仍照常落盘；`completed=False` 的流不生成快照。
- `run_local` / `record_mic` 也调用同一入口，session 用 `"local"`；测试/验证时把 `MEMORY_DIR` 指到临时目录。
- `/stop`、鉴权、chunked 行为不变；请求路径、音频格式、响应体格式不变。唯一待确认的可见变更是 `X-Session-Id` 值（§2.1）。

不改：`asr_engine.py`、`tts.py`、`llm.py`、`brains.py`、`usage.py`、`sentence_split.py`、固件、`/turn` 请求/响应体格式、鉴权、chunked。唯一待 bb96 确认的可见变更是 `X-Session-Id` 值修正（§2.1）。

## 9. 隐私与安全

- 目录在工作区外，Windows 上尽量收紧 ACL；不进入 git。
- 只存短事实/摘要/最近两轮裁剪短句；不存音频、完整对话、prompt 快照。
- 敏感模式过滤（证件/银行卡/手机号/密码/验证码/key/token/私钥/门牌）。
- LLM 记忆调用默认关闭；开启后抽取只发送本轮 user 的前 120 字（即使本轮输入上限是 500 字）+ 回复首句（≤50 字）+ 现有 L1，摘要只发送上一版摘要 + `recent_turns` + 本轮短句；不发音频、不发完整会话；可随时关闭。
- `session_id` 只用于分桶：按 §2.1 分离 + URL decode + 截 55，任意字符都接受（不白名单并到 anon）；文件名用哈希，任何字符都不拼路径；空/缺失才用 `anon`。不使用 `X-Session-Id` 头值。
- bridge 与 CLI 通过 `state.lock` 互斥写；CLI 的 `export` 只导出主动内容（facts/episodes/summary），不含 debug 日志。
- 设备端零新增存储；设备丢失不泄露记忆。
- `memory_admin.py` 提供 `delete` 与 `export`；删除是本地操作，无 HTTP 端点。
- D 的 `usage.jsonl` 不记录原文；记忆 LLM 的账本同样只记 token/费用。

## 10. 可观测性

- writer debug（仅 `MEMORY_DEBUG=1`，阶段 3 已实现）：dry-run 打 `memory: session=... dry_run=True candidates=N summary=N`；写入模式打 `memory: session=... written candidates=N`。只打计数，不打原文。
- 读路径逐轮 `L1=n L2=字符数 L3=n L4=n total=x/budget trimmed=...`（阶段 4 已实现，仅 `MEMORY_DEBUG=1`）：由 `memory_context.format_debug(info)` 生成、`pipeline._messages` 打印；只打层计数/预算/裁剪标签，不打记忆或用户原文。`memory_debug.jsonl` **仍未实现**（仅 stdout；文件落盘与轮转留待需要时再加）。
- `memory_usage.jsonl`：记忆相关 LLM 调用，字段沿用 D（`ts/profile/prompt_tokens/completion_tokens/cost_cny`），加 `purpose`（`summary`/`extract`）。尚无记忆 LLM，**文件不会产生**。
- `memory_admin.py stats`：各层条数、tombstone、待晋升、`pinned_overflow`/`at_capacity`、目录大小、最近维护时间（阶段 4 真实写入模式下启动/每回合会更新 `last_maintenance`）。
- 失败计数（阶段 3 已实现，进程内 `MemoryWriter.counters()`）：`accepted`/`rejected_ineligible`/`rejected_duplicate`/`rejected_sensitive`/`rejected_sensitive_worker`/`rejected_queue_full`/`write_errors`/`hit_errors`/`written_jobs`/`dry_run_jobs`/`dry_run_candidates`/`dry_run_summary_chars`/`hits_deferred`/`hits_flushes`/`read_only_jobs`；阶段 4 增加 `maintenance_runs`/`maintenance_errors`。**启动汇总打印尚未实现**；目前只能通过测试或进程内读取查看。

## 11. 测试计划

全部用标准库、`unittest`，不联网、不占 GPU；TDD：先写失败测试，再实现。

单元测试：

- `test_memory_session.py`：`session_id=dev1` → `sha1("dev1")` 桶，不是 anon；55 字符值完整保留；`../`、绝对路径、`%2F`、超长值不产生目录外文件；空值/缺失 → anon；URL decode 正确。`X-Session-Id` 契约测试在 bb96 确认前标记 `expectedFailure`/`skip`，不把 `d=dev1` 当成正确值。
- `test_memory_store.py`：读写 roundtrip、原子替换、并发锁、去重、**提及不计 hits**、revision/tombstone、**tombstone 被显式“记住”复活且 revision+1**、损坏行跳过与 `.bak` 恢复、**会话主文件字段非法时回退 `.bak`**、**写失败后缓存/状态失效且读结果等于磁盘**、mtime 变化重载、容量淘汰、pinned 不参与淘汰、pinned 超限进入 `at_capacity`、Windows `os.replace` 短暂占用时退避重试。
- `test_memory_extract.py`：姓名/偏好/居住/关系/待办/显式记忆正例；否定、疑问、假设、第三人称、空值反例；敏感信息拒绝；ASR 口头词；相对日期按本地时区解析。
- `test_memory_score.py`：bigram/英文混合、相似度归一、时间衰减、待办 due 加权、hits 上限、同分取新、零相似过滤、加载时 token 缓存命中/失效。
- `test_memory_context.py`：五层顺序、header、预算 **1050**、500 字本轮输入不被静默截成 120、硬裁顺序（L2 先于 L3）、L0/本轮不砍、空层、L4 只取 recent_turns 与 recent_cache 的合并、**96 字旧摘要 + 新回合时最新句仍在注入里**、pinned >2 只注入 top-2 并报 `pinned_overflow`、**L3 与 L1/前一条 L3 文本重复时按实际注入生成 l3/selected_ids**、异常回退。
- `test_memory_maintenance.py`：仅 preference 被注入 3 次才晋升（3 次提及不晋升；relation/residence/name 无晋升路径）、30/180 天降级、tombstone、幂等、容量上限、pinned 超限暂停自动写入。
- `test_memory_writer.py`（阶段 3 为 18 例；阶段 4 扩到 25 例）：完整回合写、未完成即 abort 不写、空 ASR 不写、`finish_reason=length` 不写、**clean EOF 无 `[DONE]` 且无 `finish_reason=stop` 不写**、`done=True`/`stop` 可写、**敏感回合不进入 `recent_cache` 且下一轮不注入**、**recent_cache 下一轮立即可读**、**完成回合后立即开始新 gen 仍写旧回合**、队列满丢弃、worker 异常隔离、**dry-run 零落盘**、**5 回合命中批量刷一次**、重复 gen 丢弃、缓存只留 2 轮、recent_turns 裁剪、`WriteJob` 快照不可变；阶段 4 新增：启动与每回合调用维护、计数从 state 恢复并在维护后归零、维护异常不阻塞写入、dry-run/只读不调用维护、真实维护在启动时晋升偏好、50 回合再次触发维护。
- `test_memory_admin.py`：list/pin/unpin/delete/export/stats（含 `pinned_overflow`/`at_capacity`）。
- `test_memory_integration.py`（阶段 2 读路径 16 + 阶段 3 写路径 3 + 阶段 4 可观测性/退出 3）：假 LLM + 假 TTS 的 Turn 流程；启用时注入事实/事件/摘要且 500 字输入完整保留；关闭时精确等于旧两条消息；只读模式一轮不写任何记忆文件、不产生 `state.lock`；store 异常回退；打断/新 gen 不置 `completed`；session 桶忽略 `d=dev1` 头；恶意 session_id 被哈希在 `sessions/`；`current_input_max_chars`；`/turn` 传参；`run_local`；`build_bridge` 开关；schema 过新时 WARN + 只读；非法数值环境变量回退默认。阶段 3 新增：**`/turn` 完整回合经真实 Bridge/Writer 写入 fact 与摘要、终块前 `recent_cache` 已可见、下一轮 `build_for_turn` 读到新记忆**；**dry-run 目录字节不变且 `dry_run_jobs==1`**；**敏感手机号回合不入缓存、不落盘、`rejected_sensitive==1`**。阶段 4 新增：**`MEMORY_DEBUG=1` 只打计数且不含用户/记忆原文**；**`run_local` 入队后调用 `writer.stop()`**；**`serve` 退出（含 Ctrl+C）调用 `writer.stop()` 并 `server_close()`**。

基准：

- `bench_memory_score.py`：构造 500 条 episode，跑 200 次查询，打印 p50/p95。2026-10-23 实跑（Python 3.13/Windows）：空载 seed 7/8/9 → p50 1.356/1.256/1.278 ms，p95 1.637/1.420/1.568 ms，max 1.887/1.967/2.420 ms；同机复核空载 p50=1.332 ms、p95=1.520 ms，机器有负载时 p50 1.28–1.47 ms、p95 2.1–2.8 ms、max 4.05 ms。上述为典型/负载区间，单点最优值不作为验收线；只覆盖 L3 打分 + token 缓存，不覆盖 store 读取与消息组装。

回归：

- 现有 unittest 与脚本自检全过。2026-10-23 阶段 4：**显式清单 20 个模块、350 例全过**（排除 `test_e2e.py`/`test_live.py`；它们 import 即执行真实链路）；脚本自检 22/22；记忆模块单测 **195 例**（148 + 22 集成 + 25 writer）全过。阶段 5 后为 **355 例** / 记忆 **200 例**。**不要用裸 `unittest discover`。**
- e2e（假 LLM + 真 ASR + edge，不占 GPU）跑一轮，确认首字节无明显变化、记忆写入开启/关闭行为一致。
- 不跑 `test_live.py`、不跑 CosyVoice、不占 GPU，除非用户明确同意。

手工验收：

- `MEMORY_ENABLED=1 MEMORY_DRY_RUN=1 MEMORY_DEBUG=1` 跑 3–5 轮，检查 debug 行只含计数、writer 不落盘、预算不超 1050。
- 指向临时 `MEMORY_DIR` 且 `MEMORY_DRY_RUN=0` 跑 3 轮，检查 facts/episodes/sessions 内容与去重、启动维护写入 `last_maintenance`、`turns_since_maintenance` 递增；阶段 4 已按此验收，未动真实目录。
- `memory_admin.py list/stats/delete/export`。
- 确认工作区外目录、无音频/完整对话、无敏感值。

## 12. 分阶段实施

| 阶段 | 内容 | 改动 | 可独立验收 |
|---|---|---|---|
| 1 | 纯模块：store/score/extract/context/maintenance/admin + 单测 | 只新建文件 | 已完成 2026-10-23；不接主链路 |
| 2 | 读路径接入：`memory_context` + `pipeline`/`server` 传 session | 改 3 个已有文件 + 小改 `memory_context.py` | **已验收 2026-10-23**；记忆只读不写 |
| 3 | 写路径接入：异步 writer + dry-run | 改 pipeline/server/config | **已完成 2026-10-23**；默认 dry-run、无落盘；维护未接线 |
| 4 | 打开写入 + 维护 + 可观测性 | 配置 + 维护接线 | **代码已完成 2026-10-23**：维护/读路径 debug/退出补刷；临时目录真实写入验收；默认仍 dry-run，真实目录待确认 |
| 5 | 可选 LLM 摘要/抽取 + 记账 + 限频 | 新增 memory_llm | 默认关闭 |
| 6 | HANDOFF/README/记忆更新 | 文档 | 已完成 2026-10-23 |

阶段 1 不动 bridge 行为；阶段 2 已完成只读接入；阶段 3 已完成写路径 dry-run（默认不落盘）；**阶段 4 打开真实写入与维护前需要用户逐项授权**（与 A/D 的授权方式一致）。

## 13. 风险与对策

| 风险 | 对策 |
|---|---|
| ASR 错字导致错误记忆 | 短句入库、仅 preference 被注入 ≥3 次才晋升、显式“记住”才 pin、敏感信息拒绝、CLI 可删 |
| 记忆噪声挤占上下文 | `sim>0` 门槛、top-3、1050 硬预算，L2 先裁、L3 次之 |
| 摘要质量差 | 先规则摘要；最新在前、旧摘要 38→20→0；LLM 摘要可开关、限频、失败回退 |
| 后台 LLM 增加费用 | 默认关闭；每 4–6 轮最多一次；独立账本；规则优先 |
| 本地 LLM 增强抢 GPU | 默认关闭；核心落盘后才执行；新 gen 已开始则跳过增强；独立限频/记账；可能拖慢下一轮 ASR/TTS |
| 每轮写盘磨损/延迟 | 写路径异步；读路径不写盘；核心落盘每回合最多 1 轮（每个有变更文件一次原子替换），可选 LLM 只有新增内容才二次落盘；纯命中每 5 回合刷一次；队列满丢弃 |
| 并发写坏文件 | 单 worker + 进程内锁 + `state.lock` 跨进程互斥 + tmp/replace + .bak |
| session_id 构造越界路径 | 分离 + URL decode + 截 55；任意字符接受、不并 anon；`sha1(sid)[:16]` 文件名、不拼路径；单测覆盖 `../`、绝对路径、`%2F`、超长、空值 |
| `X-Session-Id` 契约变更 | 单独列为待 bb96；记忆桶用 §2.1 独立解析器，不读该头；修复前后桶一致 |
| writer 未落盘时下一轮读到旧 recent_turns | `enqueue` 同步更新 `recent_cache`；读路径合并缓存与磁盘并去重；测试“阻塞 writer + 立即下一轮” |
| 已完成回合被新 gen abort 误判 | 不可变 `WriteJob` 快照；`completed=True` 优先，后续 abort 不回退；测试“完成回合后立即新 gen” |
| clean EOF 半截回复被写入 | 要求 `done=True` 或 `finish_reason=stop`；clean EOF 两者都无则拒绝；测试覆盖 |
| pinned 撑爆容量 | pinned 不参与自动淘汰/降级；pinned 自身超 200 进 `at_capacity`，暂停自动写入并告警；>2 只注入 top-2 并报 `pinned_overflow` |
| CLI 与 bridge 同时写丢更新 | `state.lock` 互斥；读路径按 mtime 重载，删除/置顶无需重启 |
| 记忆污染人格 | L1 只存短事实；persona 不由记忆自动改写；本地插件无权写 |
| 待办被误解为提醒 | 回复禁用“到点提醒”；设计明确待办只作对话记忆 |
| 隐私泄露 | 工作区外、无音频/完整对话、敏感过滤、删除/导出、默认 dry-run 验证 |
| 敏感内容经 recent_cache 进入下一轮 prompt | server 入队前同步预检；命中不缓存、不入队；worker 二次预检；测试覆盖 |
| 1050 预算仍超 | 最终 `estimate_tokens` 校验 + 硬裁顺序 + 单测覆盖最坏组合；本轮输入 500 不静默回退 |
| 单字/跨字查询召回不足 | 相似度为中文 bigram，`猫`/`养猫` 可能匹配不到“用户养了一只猫”；阶段 2 用真实查询评估是否加 unigram fallback，本轮不改 |
| 本轮输入仍被截断 | 默认 500，与现有 `context_builder` 一致；调低总预算必须显式同步 `MEMORY_CURRENT_INPUT_MAX_CHARS`，debug 标 `low-budget` |

## 14. 默认值与待确认项

以下为文档采用的默认值，评审时可改；标“待 bb96”的是契约事项，尚未覆盖的性能项在正文注明范围与补测阶段。

1. 输入预算 **1050**（L0–L3 共 350 + 本轮输入 500 + 历史 180 + header 20）；本轮输入上限 **500**，与现有 `context_builder` 一致，不再默认 120。
2. L1 只放 **2** 条（不是 5 条），每条 ≤30 字。
3. 摘要先规则；LLM 摘要/抽取都默认关闭。若开启且 active profile 是本地模型，需接受与下一轮 ASR/TTS 抢 GPU、拖慢响应的风险。
4. 晋升：显式“记住”立即；否则只有 `preference` 在被 L3 选中累计 3 次后晋升；relation/residence/name 直接写 `facts`，v1 无晋升路径。
5. 保留期：L1 30 天降级、180 天 tombstone；episode 180 天 tombstone。
6. active facts ≤200、episodes ≤500；pinned 不参与自动淘汰，pinned 自身超限则 `at_capacity` 暂停自动写入并告警。
7. 代码默认关闭（`MEMORY_ENABLED=0`）；上线顺序固定：阶段 2 只读 → 阶段 3 dry-run → 阶段 4 接线/临时目录真实写入验收。**阶段 1–4 代码已于 2026-10-23 验收；默认仍为 dry-run，真实 `~/.voice-ai/memory/` 写入需用户确认后再打开 `MEMORY_DRY_RUN=0`；不允许跳过 dry-run 直接写。**
8. 不提供 HTTP 记忆端点；只用本地 CLI。CLI 与 bridge 通过 `state.lock` 互斥。
9. 会话 ID 用 §2.1 独立解析（分离 + URL decode + 截 55）+ sha1 文件名；`X-Session-Id` 值修正**待 bb96**，不阻塞记忆实现。
10. 读路径延迟：L3 打分仍以 **<5 ms** 为目标（500 条基准 p50 1.26–1.47 ms、p95 1.42–1.64 ms；负载下 p95 2.1–2.8 ms、max 4.05 ms）。整条 `build_for_turn` 已实测（500 episodes + 500 字输入 + token cache 预热）：独立复核 p50 6.1–6.4 ms、p95 6.6–10.7 ms；本机复现（500 条高相似 episode）p50 11.5–13.0 ms、p95 13.3–15.0 ms、max 15.6 ms。主因是 `MemoryStore.active_episodes()` 每轮 `deepcopy` 500 条（本机 p50 5.7 ms）；同样数据浅拷贝/只读投影约 0.05–0.15 ms。**决策：不把 5 ms 作为整条读路径的硬指标**；阶段 2/3 的硬门是 p95 ≤20 ms、不整轮读盘、不新增可感知首字节延迟。阶段 3/4 未实施“只读投影/浅拷贝 + 写时复制”（未阻塞验收）；**阶段 5（2026-10-23）已实施只读投影**：`active_facts()`/`active_episodes()` 只复制顶层字段并过滤 active，调用方不得修改嵌套值。本机 500 条复测 `active_episodes` p50 3.90→0.10 ms、`build_for_turn` p50 5.72→1.93 ms / p95 6.22→2.06 ms，整条读路径已回到 <5 ms。

## 15. 验收标准

- 任意一轮最终 messages ≤1050 token；本轮输入 500 不被静默截成 120；单测覆盖最坏组合。
- L2 存储 `summary + sticky` ≤100、sticky ≤20；注入 ≤80，且 96 字旧摘要场景下最新句仍在。
- L4 历史只来自 `recent_turns` 与 `recent_cache` 的合并（≤2 轮、user ≤40/assistant ≤50）；读路径不写盘，落盘全部在异步 worker。
- 读路径新增约 6–15 ms，相对 2.4–5.3 s 的首字节预算不可感知；未单独做首字节 A/B，阶段 3 不以首字节微增为阻塞门。写全部异步。L3 打分基准典型 p50 1.26–1.47 ms、p95 1.42–1.64 ms，同机负载下 p95 2.1–2.8 ms、max 4.05 ms（500 条），仍 <5 ms。整条 `build_for_turn`：独立复核 p50 6.1–6.4 ms / p95 6.6–10.7 ms，本机高相似数据复现 p50 11.5–13.0 ms / p95 13.3–15.0 ms；**硬门为 p95 ≤20 ms**；阶段 5 已用只读投影把本机 p50 降到 1.93 ms / p95 2.06 ms（见 §20），<5 ms 目标达成，剩余为自动化回归门。
- 未完成/被打断、空 ASR、`finish_reason=length`、clean EOF 无 `[DONE]`/`stop` 的回合零写入；已完成并快照的回合不因后续新 gen 或 `/stop` 被撤销。
- 同一事实不重复；冲突保留 revision/history；删除为 tombstone；显式“记住”可复活 tombstone 且 revision+1。
- pinned 不被自动淘汰/降级；pinned 超限进入 `at_capacity` 并告警；>2 pinned 只注入 top-2 并报 `pinned_overflow`。
- 敏感值零入库；`recent_cache`/`recent_turns` 也不含敏感值；目录无音频/完整对话/prompt 快照。
- 任意 ≤55 字符 `session_id` 不产生目录外文件（哈希桶）；空/缺失才落 anon；`X-Session-Id` 修正若获 bb96 批准，桶结果前后一致；CLI 与 bridge 并发写不丢记录。
- 现有 unittest 与脚本自检全过（2026-10-23 阶段 5：显式清单 20 个模块 **355 例全过**；脚本 22/22）；记忆单测阶段 1 **148 例** + 阶段 2 集成 **16 例** + 阶段 3（writer 18 + 写路径集成 3）+ 阶段 4（writer 7 + 集成 3）+ 阶段 5（5 例）= **200 例全过**。
- `memory_admin.py list/stats/delete/export` 可用。
- 固件、`/turn` 请求/响应体、A/D/B/C 交付文件均未改；唯一待 bb96 的可见变更是 `X-Session-Id` 值。

## 16. 阶段 1 验收记录（2026-10-23）

范围：纯模块，不接主链路；未改 `config.py`、`pipeline.py`、`server.py`、固件与任何契约。以下均为 `bridge/` 下新增文件。

| 文件 | 内容 |
|---|---|
| `memory_store.py` | 原子写/`.bak`/坏行跳过、跨进程锁、facts/episodes tombstone 与 revision、显式复活、容量与 pinned、sessions、recent_turns |
| `memory_score.py` | 中文 bigram + 英文/数字分词、相似度、时间衰减、待办 due、top-3、token 缓存 |
| `memory_extract.py` | 规则抽取（姓名/偏好/居住/关系/宠物/待办/显式记忆）、敏感过滤、相对日期 |
| `memory_context.py` | 五层消息组装、1050 硬预算、L2 存储/注入算法、L4 合并去重、异常回退 |
| `memory_maintenance.py` | preference 被注入 3 次晋升、30/180 天降级、tombstone 清理、幂等、24h/50 回合节流 |
| `memory_admin.py` | list/pin/unpin/delete/export/stats/why；sticky 清理；`pending_promotion` |
| `bench_memory_score.py` | 500 条 episode × 200 次查询基准 |
| `test_memory_*.py` | 上述模块单测（含 session 解析） |

验收结果：

- 记忆单测：**148 例全过**；复核返工后连续 10 轮无失败。
- 单元回归（显式清单，排除 `test_e2e.py`/`test_live.py`）：阶段 1 当时为 148 例记忆测试全过；全量数字在阶段 2 扩到 319、阶段 3 扩到 340，当前口径见 §15 与 §18。系统 Python 导入那两个脚本会在 `funasr` 处失败，`.venv` 导入则执行真实链路，均属环境/脚本设计问题，非记忆回归。
- 脚本自检：22/22（llm 9、pipeline 5、sentence_split 8）。
- 基准（500 episodes × 200 queries，L3 打分 + token 缓存，Python 3.13/Windows）：空载 seed 7/8/9 → p50 1.356/1.256/1.278 ms，p95 1.637/1.420/1.568 ms，max 1.887/1.967/2.420 ms；同机负载下 p50 1.28–1.47 ms、p95 2.1–2.8 ms、max 4.05 ms。**L3 打分满足 <5 ms 目标**（按典型/负载区间，不取单点最优）；不含 store 读取、recent 合并与消息组装。
- 预算：单测覆盖 1050 最坏组合、500 字输入不截断、L2 先于 L3 的硬裁顺序、L0/本轮永不砍。
- 路径与隐私：session_id 哈希桶、损坏行恢复、tombstone 复活、pinned 超限、敏感预检相关单测通过。

阶段 1 发现并修复的问题：

1. `recent_turns` 曾持久化 `gen`；已从磁盘与内存结构中移除，去重键改为 `ts + user + assistant`。
2. 晋升后原 preference episode 已 tombstone，30 天降级时普通 upsert 会忽略 tombstone 造成内容丢失；降级改为 `revive=True` 复活并重置 `hits=0`，避免晋升/降级循环。
3. CLI `delete`/`purge` 待办时未清 session sticky；已补 `clear_sticky_for_todo`。
4. `stats` 未暴露待晋升数量；已补 `pending_promotion`。
5. Windows 上 `os.replace` 偶发 `WinError 5`（Defender/索引短暂占用）；已加 4 次短退避重试，并用可复现单测覆盖。

复核返工（2026-10-23）：

6. 写失败污染内存缓存/状态：`_write_file` 在任何异常时失效对应 facts/episodes 缓存，`save_state` 改为写成功后才更新 `_state`，`bump_counter` 持久化失败不保留脏状态；单测打桩 `OSError` 校验“读结果 == 磁盘”。
7. 会话 `.bak` 未在字段非法时生效：`completed_turns` 经 `_parse_completed_turns` 防御转换，非法/负值返回 `None` 触发 `.bak`/默认；`save_session` 非法值回 0；补“主文件坏字段 + 有效 `.bak`”单测。
8. 晋升计数对齐 §7：新 facts/episodes `hits=0`、`last_used_at=None`，提及不再 +1，仅 `mark_used`（L1/L3 实际注入）累加；晋升/降级测试改为“3 次注入晋升、3 次提及不晋升”。
9. L3 去重计数：`_assemble` 返回实际注入的 facts/episodes，`info["l1"]`/`l3`/`selected_ids` 只统计真正进消息的条目；补重复文本单测，避免后续阶段给未注入项 `mark_used`。
10. 晋升目标 fact 仍为 tombstone（如同 slot 已 `demoted`）时，原先仍会 tombstone 源 episode，导致内容从活跃层消失；现仅当 `upsert_fact` 返回 `status=="active"` 才删除 episode 并计数，否则保留 episode 待显式处理（不自动复活 `demoted`/`deleted`）；补回归单测。
11. 跨实例 state 丢更新：`bump_counter`/`set_at_capacity`/`enforce_capacity` 改为 `update_state(mutator)`，在 `_write_guard` 内重读 `state.json` 后再修改，避免用旧 `_state` 覆盖其他实例写入的 counters/at_capacity；补双实例回归单测。
12. 文档澄清：`maybe_run(completed_turns=...)` 传“距上次维护的增量”而非会话累计；基准改为典型/负载区间（负载下 p95 2.1–2.8 ms、max 4.05 ms，仍 <5 ms）；§13 记录中文 bigram 对单字/跨字查询的已知召回限制，阶段 2 再评估 unigram fallback。

阶段 1 未包含（后续阶段状态）：

- `memory_writer.py`（异步写队列、不可变 `WriteJob`、`recent_cache`）：阶段 3 **已完成**（见 §18）。
- `test_memory_integration.py`：阶段 2 **已完成**；阶段 3 扩到 19 例。
- 读路径不自动计命中：`build_for_turn` 不调用 `mark_used`。阶段 2 的授权范围明确排除写盘与命中计数，**因此阶段 2 没有实现 `mark_used`**；阶段 3 已在真实写入模式下按 `selected_ids` 批量 `mark_used`，dry-run 仍不计数（只记 `hits_deferred`）。
- 完整读路径端到端计时：已补测，见 §14 第 10 条与 §15；硬门 p95 ≤20 ms，`active_episodes()` 深拷贝曾是主因；**阶段 5 已完成只读投影优化**（见 §20）。
- `MEMORY_*` 配置项、server/pipeline 会话传递：阶段 2 **已完成**；dry-run 与 writer：阶段 3 **已完成**。

阶段 2 决策与待决：

1. 1050/500、L2 注入 80/存储 100/sticky 20、pinned 超限策略、LLM 增强默认关闭：已按本设计落地。
2. bb96：`X-Session-Id` 可见值 `d=dev1` → `dev1` 仍待确认；记忆桶不依赖该头，不阻塞后续阶段。
3. 用户：阶段 3 已于 2026-10-23 授权并完成（`memory_writer.py`、`WriteJob`、`recent_cache`、敏感预检、dry-run、`mark_used` 批量刷）；维护接线未做，留阶段 4。
4. 硬化清单：`active_episodes()` 只读投影/浅拷贝与 `MemoryStore._sessions`、`RecentCache._items`、`MemoryWriter._seen_gens` 上限**已于阶段 5 完成**（见 §20）；集成测试 2 处 `d=dev1` 硬断言已在阶段 3 改为契约断言；读路径 p95 ≤20 ms 回归门尚未自动化。

## 17. 阶段 2 验收记录（2026-10-23）

范围：只读接入。改了 `config.py`、`memory_context.py`（小改 `current_input_max_chars`）、`pipeline.py`、`server.py`，新增 `test_memory_integration.py`；未改固件、`/turn` 请求/响应体、鉴权、chunked，未改 `X-Session-Id` 可见值。

### 17.1 实际改动

- `config.py` 只加 4 项：`MEMORY_ENABLED=0`、`MEMORY_DIR`、`MEMORY_INPUT_BUDGET_TOKENS=1050`、`MEMORY_CURRENT_INPUT_MAX_CHARS=500`。阶段 2 时**没有** `MEMORY_LLM` / `MEMORY_DRY_RUN` / `MEMORY_DEBUG`；后两项已在阶段 3 补上（§18），`MEMORY_LLM` 仍无。
- `pipeline.Turn` 增加 `session_id`、`reply_text`、`completed`；`on_content` 累积回复（stale 忽略）；正常结束时置 `completed=True`，abort/stale/新 gen 不置。
- `pipeline.Turn._messages()`：仅当 `MEMORY_ENABLED=1` 且 `bridge.memory_store` 存在时调用 `build_for_turn`；`info["fallback"]` 或异常时回退旧两条消息。
- `Bridge` 增加 `memory_store` / `token_cache`；`begin(..., session_id="anon")` 默认兼容旧调用。
- `server.build_bridge()` 只在启用时构造 `MemoryStore` 与 `TokenCache`；`/turn` 启用时传 `parse_session_id` 结果，关闭时仍两参数 `begin`；`run_local` / `record_mic` 启用时用 `session_id="local"`。
- `X-Session-Id` 响应头仍由 `_session()` 返回 `d=dev1`；记忆桶用 `dev1`。

### 17.2 只读证据

- 阶段 2 不调用 `mark_used`、不创建 `MEMORY_DIR`、不写 facts/episodes/sessions、不跑维护、不落摘要/最近轮次、不创建 `state.lock`。
- 测试断言：启用一轮后记忆目录文件字节不变；关闭一轮时用 `ExplodingStore` 证明完全不读记忆；开启/关闭两种消息构造都有断言。
- 恶意 `session_id=../../evil` 被哈希到 `sessions/` 内，没有目录外文件。
- 阶段 2 修改文件 mtime 为 2026-10-23 02:27–02:43（`config.py`/`server.py` 为最后两处）；受保护的 A/D/B/C 源文件早于 02:17，固件未动。

### 17.3 测试与演示

- `test_memory_integration.py`：**16/16**，连续 10 轮无失败；覆盖启用注入、关闭旧行为、文件不变、store 异常回退、打断不完成、新 gen 取代、`dev1` vs `d=dev1`、路径穿越、200 字上限、`/turn` 传参、`run_local`、`build_bridge` 开关、配置类型、`schema_version` 过新的只读告警、非法数值环境变量回退。
- 记忆合计：148（阶段 1）+ 16（阶段 2）= **164 例全过**（阶段 3 后为 185，见 §18）。
- 单元回归（显式清单 19 个模块，排除 `test_e2e.py`/`test_live.py`）：**319 例全过**（阶段 3 后为 20 模块 / 340，见 §18）。裸 discover 会 import 即执行真实 HTTP/ASR/TTS/云端调用；系统 Python 下报 2 个 `funasr` import error，`.venv` 下 `test_live` 会启动 CosyVoice 并写账本。**不要用裸 discover。**
- 假 LLM/TTS 演示（启用）：system 含事实/事件/摘要；用户 500 字未被截断；总 **565 token**（≤1050）；`completed=True`；回复 `好的。`。
- 关闭演示：消息精确等于 `[system DEFAULT_PERSONA, user text]`，没有读记忆。

### 17.4 阶段 2 的已知偏差

- 设计 §8 早期草稿提过“阶段 2 由 server 按 `selected_ids` 调 `mark_used(persist=False)`”；最终授权范围排除了写盘/命中计数，**该调用没有实现**。这是有意的范围收口，不是遗漏。§5 与 §16 的旧表述已按此统一。
- 后果：阶段 2 只能验证“读路径正确、预算正确、关闭无副作用”，不能验证晋升/降级/真实积累；阶段 3 dry-run 仍不能验证这些，留阶段 4 真实写入。
- 整条读路径已实测并记录在 §14/§15：p95 超过 5 ms 但远低于首字节预算；硬门定为 p95 ≤20 ms，<5 ms 作为后续优化目标（阶段 5 已达成，见 §20）。
- 独立复核时为集成 **14 例 / 记忆 162 例**；复核后补了 “schema 过新只读告警”和“非法数值环境变量回退”两个防护测试，阶段 2 结束时为 **16 / 164**。复核报告中的 318/319 计数也对应旧快照；阶段 2 结束时 hermetic 单元回归为 **319 例全过**，阶段 3 后为 **340**（§18）；阶段 5 后为 **355**（§20）。
- 复核提出的两项硬化已在当前代码解决：`build_bridge` 对更高 `schema_version` 打印 `WARN ... read-only`；`MEMORY_*` 等数值环境变量非法时打印 `WARN` 并回退默认值，不再 import 崩溃。
- `test_memory_integration.py` 的 2 处 `X-Session-Id == "d=dev1"` 硬断言已在阶段 3 解耦为契约断言（只要求头部存在，且记忆桶与头部值解耦），bb96 修正 `_session()` 后不会误报。
- ~~仍未解决、留阶段 4：`MemoryStore._sessions` 缓存无上限~~ **阶段 5 已加 64 会话 LRU 上限（见 §20）。**

### 17.5 下一步（阶段 2 当时）

阶段 3：`memory_writer.py` + 不可变 `WriteJob` + `recent_cache` + 敏感预检 + dry-run + 批量 `mark_used` + 维护接入；先只记录不落盘，验收后再进阶段 4 真实写入。开工前需用户授权，仍不改固件、不改 `/turn` 契约。

> 更新：阶段 3 已于 2026-10-23 完成（维护接线除外），验收记录见 §18。

## 18. 阶段 3 验收记录（2026-10-23）

范围：写路径 dry-run。改 4 个已有文件（`config.py`、`pipeline.py`、`server.py`、`test_memory_integration.py`），新增 2 个文件（`memory_writer.py`、`test_memory_writer.py`）；未改 `memory_context.py`、固件、`/turn` 请求/响应体、鉴权、chunked，未改 `X-Session-Id` 可见值。**维护未接线；没有调用 `MemoryMaintenance.maybe_run`。**

### 18.1 实际改动

- `config.py`：新增 `MEMORY_DRY_RUN`（默认 `"1"` → dry-run）与 `MEMORY_DEBUG`（默认 `"0"`）。`MEMORY_LLM` 仍未加入。
- `memory_writer.py`（新）：`is_eligible`、frozen `WriteJob`、`RecentCache`、`MemoryWriter`。常量 `QUEUE_MAXSIZE=64`、`RECENT_TURNS=2`、`HIT_FLUSH_TURNS=5`、`SHUTDOWN_WAIT_SECONDS=2.0`、`MIN_CONFIDENCE=0.7`。
- `pipeline.py`：`Turn` 增加 `selected_ids`（来自读路径实际注入的 id）；`_messages` 把 `writer.recent_cache.get(session_id)` 传给 `build_for_turn`；`Bridge` 增加 `memory_writer`。
- `server.py`：`build_bridge` 构造 `MemoryWriter(store, dry_run=config.MEMORY_DRY_RUN or store.read_only, debug=config.MEMORY_DEBUG)`；`enqueue_memory(bridge, turn)` 统一入口，异常只打印 `WARN`；`/turn` 在 PCM 循环结束、**终块 `self._chunk(b"")` 之前**入队；`run_local` / `record_mic` 在 PCM 循环后入队。
- `test_memory_integration.py`：19 例（原 16 + 写路径 3）；`X-Session-Id` 两处硬断言改为契约断言；`build_bridge` 断言 writer 存在且默认 `dry_run=True`。
- `test_memory_writer.py`（新）：18 例。

### 18.2 写路径语义

- 资格：`completed=True`、无 `error`、`finish_reason != "length"`、`done=True` 或 `finish_reason=="stop"`、user/assistant 非空。`completed` 一旦置位，后续 abort/new gen 不撤销快照。
- 入队顺序：资格 → 重复 gen 检查 → **同步敏感预检（user+assistant）** → `recent_cache.record` → 非阻塞 `put_nowait`。敏感或队列满都不影响回复；队列满丢弃并计数。
- worker：二次敏感预检 → 规则抽取（`memory_extract.extract`，只读 user）→ confidence ≥0.7 且候选自身不含敏感信息。dry-run 只组装会话预览并计数，不写 facts/episodes/sessions、不调 `mark_used`，命中记 `hits_deferred`。
- 真实写入模式（需 `MEMORY_DRY_RUN=0` 且 store 非只读）：`_write_candidates` 写 facts/episodes，`_assemble_session` 生成 summary/sticky/recent_turns 并 `save_session`；`selected_ids` 进入 `_pending_hits`，每 5 个完成回合批量 `mark_used`（除最后一条 `persist=False`，只落盘一次），`stop()` 时补刷。
- `store.read_only`（`schema_version` 过新）强制 dry-run，即使 `MEMORY_DRY_RUN=0`。
- 读路径仍不写盘；dry-run 下 `recent_cache` 仍会在进程内影响下一轮 prompt（与阶段 2 的严格只读不同），但不落盘。

### 18.3 测试与证据

- `test_memory_writer.py`：**18/18**。覆盖完成回合写 + 摘要、偏好/待办路由与 sticky、未完成/clean EOF/length/空内容拒绝、完成回合抗后续 abort/new gen、敏感回合不缓存不入队、recent_cache 下一轮可见、队列满、worker 异常隔离、dry-run 零落盘、5 回合命中批量刷一次、重复 gen、缓存 2 轮、recent_turns 裁剪、`WriteJob` 冻结。
- `test_memory_integration.py`：**19/19**。新增：真实 Bridge/Writer 经 `/turn` 写入 fact 与摘要、终块前 cache 已可见、下一轮读到新记忆；dry-run 目录字节不变且 `dry_run_jobs==1`；敏感手机号回合空缓存、零落盘、`rejected_sensitive==1`。
- 记忆合计：148（阶段 1）+ 19（集成）+ 18（writer）= **185 例全过**。
- 单元回归（显式清单 20 个模块，排除 `test_e2e.py`/`test_live.py`）：**340 例全过**；脚本自检 **22/22**。
- 文件 mtime：阶段 3 修改集中在 2026-10-23 02:57–02:59（`memory_writer.py` 02:58、`config.py`/`pipeline.py` 02:58、`server.py` 02:58、`test_memory_integration.py` 02:58、`test_memory_writer.py` 02:57）；`memory_context.py` 仍为 02:27；A/D/B/C 源文件早于 02:17；固件无新于 03:00 的文件。

### 18.4 阶段 3 的已知偏差与未完成

> 更新（阶段 4）：维护接线、读路径 stdout debug、退出补刷三项已解决，见 §19；其余项仍有效。

- **维护未接线**：`memory_maintenance.maybe_run` 没有被调用，`completed_turns` 只在真实写入时累加；晋升/降级/容量清理要等阶段 4。
- **读路径 debug 未实现**：`MEMORY_DEBUG=1` 当前只控制 writer 的计数输出；设计的逐轮 `L1=...` 行与 `memory_debug.jsonl` 没有实现。
- **`MEMORY_LLM` 未实现**：抽取与摘要仍为纯规则；`memory_usage.jsonl` 不会产生。
- ~~`MemoryStore._sessions` 无 LRU/上限；`MemoryWriter._seen_gens` 也随进程内回合数增长；`RecentCache._items` 按 session 数增长。~~ **阶段 5 已加界（见 §20）。**
- ~~`MemoryWriter.stop()` 已实现，但 `serve()` 没有退出钩子调用它；真实写入模式下进程退出时的命中补刷尚未接线（dry-run 无影响）。~~ **阶段 4 已接线。**
- ~~读路径 `active_episodes()` 深拷贝优化未做~~ **阶段 5 已完成**；p95 ≤20 ms 硬门仍由既有实测支撑，未加自动化回归门。
- dry-run 不统计真实命中，只累计 `hits_deferred`；晋升路径仍未在真实数据上验证。

### 18.5 下一步

阶段 4：打开 `MEMORY_DRY_RUN=0` 前先备份 `~/.voice-ai/memory/`；接线维护、读路径 debug、退出补刷；再评估 `active_episodes()` 浅拷贝/COW 与 session 缓存上限。开工前需用户授权，仍不改固件、不改 `/turn` 契约。

> 更新：阶段 4 代码接线已于 2026-10-23 完成并通过临时目录验收，见 §19；真实目录默认仍未打开。阶段 5 的读路径与缓存上限优化于同日完成，见 §20。

## 19. 阶段 4 验收记录（2026-10-23）

范围：维护接线、读路径 debug、退出补刷。默认仍 `MEMORY_ENABLED=0` + `MEMORY_DRY_RUN=1`；只在临时目录做真实写入验收，未创建、未写入 `~/.voice-ai/memory/`。未改 `config.py`、`memory_maintenance.py`、固件、`/turn` 请求/响应体、鉴权、chunked、`X-Session-Id` 可见值。

### 19.1 实际改动

- `memory_writer.py`：`MemoryWriter(..., maintenance=None)`；启动时从 `store.state["turns_since_maintenance"]` 恢复“距上次维护的增量”；真实写入模式下启动评估一次 `maybe_run(now=..., completed_turns=None)`，每个成功写盘回合把计数 +1 后调用 `maybe_run(completed_turns=count)`，维护运行后计数归零；新增 `maintenance_runs`/`maintenance_errors` 计数；维护异常只计数，不影响写盘与回复。dry-run 与 `store.read_only` 完全不调用维护——`maybe_run` 即使未到期也会在有 `completed_turns` 时保存 state，dry-run 下必须避免这次写盘。
- `memory_context.py`：新增 `format_debug(info)`，输出 `L1/L2/L3/L4/total/budget/trimmed/current/warnings`；fallback 行只含异常信息，不含原文。
- `pipeline.py`：`_messages` 在 `MEMORY_DEBUG=1` 时打印上述单行。
- `server.py`：`build_bridge` 传入 `MemoryMaintenance(store)`；新增 `stop_memory(bridge)`；`serve` 加 `try/except KeyboardInterrupt/finally`，退出时 `writer.stop()` 后 `server_close()`；`run_local`/`record_mic` 在入队后调用 `stop_memory`。
- 测试：`test_memory_writer.py` 18→25；`test_memory_integration.py` 19→22。

### 19.2 语义

- 维护计数器是“距上次维护新增的完成回合数”，不是会话累计；重启后从 state 恢复，因此 50 回合节流跨进程成立。
- 启动评估按 `last_maintenance is None`、≥24 h 或持久化计数 ≥50 触发；首次真实写入启动会立即跑一次维护并写 `last_maintenance`。
- 未到期的 `maybe_run(completed_turns=...)` 会把增量写回 `state.json`；这是真实写入模式的预期落盘，dry-run 不会发生。
- 退出补刷：`stop()` 把 STOP 放在队列尾部，先处理已入队任务；真实模式随后补刷命中。
- `MEMORY_DEBUG` 只打计数；`memory_debug.jsonl` 未实现。

### 19.3 测试与证据

- `test_memory_writer.py` **25/25**：原 18 例 + 启动/每回合维护、计数恢复与归零、维护异常隔离、dry-run/只读不调用维护、真实维护启动晋升、50 回合再次触发。
- `test_memory_integration.py` **22/22**：原 19 例 + debug 只打计数不含原文、`run_local` 入队后停止 writer、`serve` 退出停止 writer 并关闭 server。
- 记忆合计 **195 例**（148 + 22 集成 + 25 writer）全过；hermetic 20 模块 **350 例**；脚本自检 **22/22**。
- 临时目录真实写入演示（`MEMORY_DRY_RUN=0`，3 个完成回合）：生成 `facts.jsonl`/`episodes.jsonl`/`sessions/<hash>.json`/`state.json`（各含 `.bak`）；`written_jobs=3`、`maintenance_runs=1`、`last_maintenance` 已写、`turns_since_maintenance=3`、session `completed_turns=3`；`~/.voice-ai/memory/` 仍不存在。
- 文件 mtime：`memory_writer.py`/`memory_context.py`/`pipeline.py`/`server.py` 为 03:13；`test_memory_integration.py` 03:12、`test_memory_writer.py` 03:13；`config.py` 仍 02:58、`memory_maintenance.py` 仍 02:17。

### 19.4 仍未做

- 真实 `~/.voice-ai/memory/` 未写入、默认未改；打开 `MEMORY_DRY_RUN=0` 前需用户确认并备份。
- `memory_debug.jsonl`、启动汇总打印未实现。
- `MEMORY_LLM`/`memory_usage.jsonl` 仍无。
- ~~`active_episodes()` 浅拷贝/COW、`MemoryStore._sessions`/`MemoryWriter._seen_gens`/`RecentCache._items` 上限~~ **阶段 5 已完成（见 §20）**；读路径 p95 ≤20 ms 自动回归门仍未做。
- dry-run 仍不统计真实命中（只累计 `hits_deferred`）。

## 20. 阶段 5 验收记录（2026-10-23）

范围：阶段 4 遗留的读路径与进程内缓存上限；不改变任何对外语义（`/turn`、`X-Session-Id`、固件、默认开关均未动），默认仍 `MEMORY_ENABLED=0` + `MEMORY_DRY_RUN=1`。

### 20.1 实际改动

- `memory_store.py`：`active_facts()`/`active_episodes()` 不再整表 `deepcopy`，改为在文件锁内过滤 active 并只复制每条记录的顶层字段（返回值是只读投影，调用方不得修改嵌套的 `history`）；`facts()`/`episodes()` 保持深拷贝，供维护/管理命令使用。
- `memory_store.py`：`MemoryStore(..., max_sessions=64)`；`_sessions`/`_session_stat` 按 bucket 做 LRU（读写都刷新顺序，超限淘汰最旧），加 `_sessions_guard` 避免并发读写字典。
- `memory_writer.py`：`RecentCache(max_sessions=64)` 按 session 做 LRU；`MemoryWriter(..., seen_gen_limit=8192)` 用 `deque` + `set` 给去重集合加 FIFO 上限；超限淘汰最老 gen（gen 单调递增，正常不会复用）。
- 测试：`test_memory_store.py` +3、`test_memory_writer.py` +2。

### 20.2 基准与测试证据

- 本机 500 条 active episode（全部写在 `episodes.jsonl`，500 字输入，token cache 预热）：`active_episodes()` p50 **3.904→0.104 ms**、p95 4.419→0.111 ms；`build_for_turn` p50 **5.715→1.927 ms**、p95 6.217→2.058 ms。整条读路径已 <5 ms。
- 新增测试覆盖：只读投影不能从返回 dict 改到 store、`active_episodes` 至少比整表深拷贝快 4×（实测约 37×）、session 缓存上限与磁盘回读、`RecentCache` 会话 LRU、seen-gen FIFO 上限。
- 记忆合计 **200 例**（148 + 22 集成 + 25 writer + 5 阶段 5）全过；hermetic 20 模块 **355 例**；脚本自检 **22/22**。
- 未改 `config.py`、`server.py`、`pipeline.py`、`memory_context.py`、固件；`~/.voice-ai/memory/` 未创建。

### 20.3 仍未做

- 真实目录启用（待确认 + 备份）。
- 读路径 p95 ≤20 ms 自动回归门（当前由单元测试的相对性能断言 + 手工基准支撑）。
- `memory_debug.jsonl`、`MEMORY_LLM`/`memory_usage.jsonl`。
