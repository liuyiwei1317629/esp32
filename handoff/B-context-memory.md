# B 包 · 上下文 / 长期记忆纯模块

> 把这份文件整段复制给负责 B 包的 AI。你只需要交付两个新文件，不要做别的包。
> 如果用户说「停 / 停停」，立即停止当前操作，不要再写文件或运行命令。

## 1. 背景

这是一个本地语音陪伴项目。每轮对话的链路是：

`ESP32 录音 → PC bridge → SenseVoice 识别 → 云端 LLM → 按句切分 → TTS → PCM 回设备`

云端 LLM 是唯一的「人格大脑」。每轮只给它很小的上下文，不塞 DSH 的系统提示、工作区文件、工具定义或完整会话历史。上下文预算：

1. 人格设定（≤120 token，中文按 1 字≈1 token 估算）
2. 检索到的 top-3 长期记忆（每条 ≤30 字）
3. 最近 2 轮已完成的对话 + 100 字摘要
4. 本轮用户输入

当前 `bridge/pipeline.py` 只发送「system persona + 当前 user」，没有历史、没有记忆。B 包负责把上下文组装逻辑做成**纯函数模块**，之后由集成会话接进 `pipeline.py`。

## 2. 文件边界（只允许新建这两个文件）

```
C:\Users\DELL\voice-ai\bridge\context_builder.py
C:\Users\DELL\voice-ai\bridge\test_context_builder.py
```

- **只新建**，不得修改、删除、重命名任何已有文件。
- 不要动 `config.py`、`llm.py`、`pipeline.py`、`server.py`、`tts.py`、`asr_engine.py`。
- 不要创建 README、示例数据、日志或其他文件。
- 不要修改 `HANDOFF.md` 或 `handoff/` 下的任何文件。

## 3. 运行环境

- Python：`C:\Users\DELL\voice-ai\.venv\Scripts\python.exe`（3.13.16）。
- 测试从 `C:\Users\DELL\voice-ai\bridge` 目录运行。
- **只用 Python 标准库**。不要安装任何包，不要 import torch/funasr/requests/config/llm/pipeline。
- 不联网、不调用模型、不读写密钥、不占 GPU、不读写真实会话文件。

## 4. 必须实现的 API

模块 `context_builder.py` 至少提供以下名称和签名。可以加私有辅助函数，但不要改这些名字。

```python
PERSONA_MAX_CHARS = 120
MEMORY_MAX_ITEMS = 3
MEMORY_MAX_CHARS = 30
SUMMARY_MAX_CHARS = 100
HISTORY_TURNS = 2
USER_MAX_CHARS = 120
ASSISTANT_MAX_CHARS = 120
CURRENT_INPUT_MAX_CHARS = 500


def clip_text(text, limit: int) -> str:
    ...


def trim_history(history, max_turns: int = HISTORY_TURNS) -> list[dict]:
    ...


def appended_history(history, user: str, assistant: str,
                     max_turns: int = HISTORY_TURNS) -> list[dict]:
    ...


def select_memories(query: str, memories, k: int = MEMORY_MAX_ITEMS) -> list[str]:
    ...


def build_messages(persona: str, history, user_text: str,
                   memories=(), summary: str = "") -> list[dict]:
    ...


def estimate_tokens(text: str) -> int:
    ...
```

### 4.1 `clip_text`

- `None` → `""`。
- 先 `str(text).strip()`；`limit <= 0` → `""`。
- 截到前 `limit` 个字符，再去掉尾部空白；不要加省略号（省略号也占预算）。
- 不修改输入（字符串不可变，但不要依赖调用方）。

### 4.2 `trim_history`

- 输入是轮次列表，每项格式：`{"user": str, "assistant": str}`。
- 过滤掉：不是 `dict`、缺键、值不是 `str`、`user` 或 `assistant` 去空白后为空。
- 返回**最后** `max_turns` 轮；`max_turns <= 0` 或输入为 `None` → `[]`。
- 返回新的 list 和新的 dict；不要原地修改输入。
- 不在这一步做长度截断（交给 `build_messages`）。

### 4.3 `appended_history`

- 先对现有历史做 `trim_history(history, max_turns)`。
- 如果 `user` 或 `assistant` 不是字符串、或去空白后为空：**不追加**，直接返回整理后的历史副本。
- 否则追加 `{"user": user.strip(), "assistant": assistant.strip()}`，再保留最后 `max_turns` 轮。
- 不原地修改输入。
- 语义：只应在「本轮完整结束」后调用；空识别、被打断、没有完整回复的轮次不要调用。

### 4.4 `select_memories`

长期记忆是外部存储的短句列表。这里只用**确定性的字面匹配**，不要模型、不要向量、不要联网。

输入：

- `memories` 可以是字符串列表，也可以是 `{"text": str, ...}` 的列表；跳过空文本和非字符串。
- `query` 是当前用户输入。
- `k` 默认 3；`k <= 0` 或 query 为空 → `[]`。

归一化：

- `unicodedata.normalize("NFKC", text).lower()`。
- 中文：把连续中文串切成**字符 bigram**（长度 1 的串就用单字）。例如「今天天气」→ `今天`、`天天`、`天气`。
- 英文/数字：按非字母数字切分，得到 token。
- 标点和空白不参与。

打分：

- 记忆 token 集合与 query token 集合的交集大小。
- 只保留分数 > 0 的记忆。
- 按分数降序；分数相同时，**列表里越靠后（越新）越优先**。
- 返回最多 `k` 条**原始文本**（strip 后，不截断）；截断由 `build_messages` 做。

这是刻意的轻量方案：宁可少召回，也不要把无关记忆塞进上下文。

### 4.5 `build_messages`

这是集成会话唯一需要调用的入口。返回 OpenAI Chat Completions 格式：

```python
[
  {"role": "system", "content": "..."},
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "..."},
  {"role": "user", "content": "本轮输入"},
]
```

规则：

1. `persona` 去空白后为空 → `ValueError`。
2. `user_text` 去空白后为空 → `ValueError`。
3. system 内容按顺序拼：
   - `clip_text(persona, PERSONA_MAX_CHARS)`
   - 若有记忆：空行 + `长期记忆：` + 每行 `- ` + `clip_text(memory, MEMORY_MAX_CHARS)`
   - 若有摘要：空行 + `之前聊到：` + `clip_text(summary, SUMMARY_MAX_CHARS)`
   - 记忆先用 `select_memories(user_text, memories, MEMORY_MAX_ITEMS)` 选，再截断。
4. 历史用 `trim_history(history, HISTORY_TURNS)`；每条 user/assistant 分别 `clip_text(..., USER_MAX_CHARS/ASSISTANT_MAX_CHARS)`。
5. 最后一条是当前用户输入，`clip_text(user_text, CURRENT_INPUT_MAX_CHARS)`。
6. 不修改 `history`、`memories`、`summary` 或 `persona`。
7. 不要输出空的 system/user/assistant 消息；历史项若截断后为空则跳过（正常不会发生）。
8. 不要在这里调用 `config.DEFAULT_PERSONA`；人格由调用方传入。

### 4.6 `estimate_tokens`

- 粗略估算，不追求和 tokenizer 完全一致。
- 中文/CJK 字符按 1 token/字；其他非空白字符按 4 字符≈1 token，向上取整。
- 空文本 → 0。
- 用于集成层做预算日志，不作为硬限制。

## 5. 测试要求

`test_context_builder.py` 用 `unittest`，从 `bridge` 目录直接可跑。至少覆盖以下 24 类行为：

1. `clip_text` 正常截断、去尾部空白、`limit<=0`、`None`。
2. `clip_text` 不添加省略号。
3. `trim_history` 只保留最后 2 轮。
4. `trim_history` 过滤缺键/空字符串/非 dict。
5. `trim_history` 不修改输入 list/dict。
6. `appended_history` 追加一轮并保留最后 2 轮。
7. `appended_history` 在 user 为空时不追加。
8. `appended_history` 在 assistant 为空时不追加（被打断的轮次不算）。
9. `select_memories` 选 top-3。
10. `select_memories` 排除 0 分记忆。
11. `select_memories` 同分时后出现的优先。
12. `select_memories` 支持字符串和 `{"text": ...}` 两种输入。
13. `select_memories` 在 query 为空时返回 `[]`。
14. `build_messages` 输出顺序：system → 历史 user/assistant → 当前 user。
15. `build_messages` 的 persona 截到 120 字。
16. `build_messages` 每条记忆截到 30 字，最多 3 条。
17. `build_messages` 的摘要截到 100 字。
18. `build_messages` 的历史 assistant 截到 120 字。
19. `build_messages` 不把当前输入放进历史。
20. `build_messages` 不修改输入。
21. `build_messages` 对空 persona / 空 user_text 抛 `ValueError`。
22. `estimate_tokens` 对空串返回 0，对纯中文单字递增。
23. 中文标点、全角字符、前后空白不影响解析。
24. 长输入被 `CURRENT_INPUT_MAX_CHARS` 截断。

测试里不要读取真实文件、不要用网络、不要 sleep。用固定字符串断言。

### 验收命令

```bash
cd /c/Users/DELL/voice-ai/bridge
../.venv/Scripts/python.exe -m unittest -v test_context_builder
```

要求：全部通过，退出码 0。把输出最后 15 行贴回报告。

## 6. 禁止事项

- 不要修改任何已有文件；不要碰 `bridge/config.py`、`llm.py`、`pipeline.py`、`server.py`。
- 不要写持久化、不要建数据库、不要读写 `~/.deepseek`、`~/.voice-ai` 或任何真实用户数据。
- 不要调用云端/本地模型、不要下载模型、不要占 GPU。
- 不要把人格、记忆写进代码常量；人格由调用方传入。
- 不要擅自扩大上下文预算；120/3/30/100/2 这些数字是产品约束。
- 不要为了让测试通过而修改测试期望；如果需求有歧义，选最简单、最保守的行为并在报告里说明。

## 7. 报告格式

```text
包名：B
模型/思考强度：
创建的文件：
确认未修改的已有文件：
测试命令：
测试输出尾部：
设计说明（选记忆算法/截断顺序）：
未验证/阻塞：
需要集成会话做的事：
```
