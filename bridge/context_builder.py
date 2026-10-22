"""语音陪伴桥接层：每轮上下文的纯组装模块。

不持久化、不联网、不调用模型；人格由调用方传入。
"""

import unicodedata

PERSONA_MAX_CHARS = 120
MEMORY_MAX_ITEMS = 3
MEMORY_MAX_CHARS = 30
SUMMARY_MAX_CHARS = 100
HISTORY_TURNS = 2
USER_MAX_CHARS = 120
ASSISTANT_MAX_CHARS = 120
CURRENT_INPUT_MAX_CHARS = 500

MEMORY_HEADER = "长期记忆："
SUMMARY_HEADER = "之前聊到："


def clip_text(text, limit):
    if text is None or limit <= 0:
        return ""
    return str(text).strip()[:limit].rstrip()


def _strip(value):
    if value is None:
        return ""
    return str(value).strip()


def trim_history(history, max_turns=HISTORY_TURNS):
    if history is None or max_turns <= 0:
        return []
    valid = []
    for item in history:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        assistant = item.get("assistant")
        if not isinstance(user, str) or not isinstance(assistant, str):
            continue
        if not user.strip() or not assistant.strip():
            continue
        valid.append({"user": user, "assistant": assistant})
    return valid[-max_turns:]


def appended_history(history, user, assistant, max_turns=HISTORY_TURNS):
    result = trim_history(history, max_turns)
    if not isinstance(user, str) or not isinstance(assistant, str):
        return result
    if not user.strip() or not assistant.strip():
        return result
    if max_turns <= 0:
        return []
    result.append({"user": user.strip(), "assistant": assistant.strip()})
    return result[-max_turns:]


def select_memories(query, memories, k=MEMORY_MAX_ITEMS):
    if memories is None or k <= 0 or not _strip(query):
        return []
    query_tokens = _tokens(query)
    scored = []
    for index, item in enumerate(memories):
        text = _memory_text(item)
        if not text:
            continue
        overlap = len(_tokens(text) & query_tokens)
        if overlap:
            scored.append((-overlap, -index, text))
    scored.sort()
    return [entry[2] for entry in scored[:k]]


def build_messages(persona, history, user_text, memories=(), summary=""):
    persona = _strip(persona)
    user_text = _strip(user_text)
    if not persona:
        raise ValueError("persona 不能为空")
    if not user_text:
        raise ValueError("user_text 不能为空")

    parts = [clip_text(persona, PERSONA_MAX_CHARS)]

    selected = select_memories(user_text, memories, MEMORY_MAX_ITEMS)
    if selected:
        lines = [MEMORY_HEADER]
        lines += ["- " + clip_text(m, MEMORY_MAX_CHARS) for m in selected if clip_text(m, MEMORY_MAX_CHARS)]
        if len(lines) > 1:
            parts.append("\n".join(lines))

    summary_clip = clip_text(summary, SUMMARY_MAX_CHARS)
    if summary_clip:
        parts.append(SUMMARY_HEADER + summary_clip)

    messages = [{"role": "system", "content": "\n\n".join(parts)}]
    for item in trim_history(history, HISTORY_TURNS):
        user = clip_text(item["user"], USER_MAX_CHARS)
        assistant = clip_text(item["assistant"], ASSISTANT_MAX_CHARS)
        if user:
            messages.append({"role": "user", "content": user})
        if assistant:
            messages.append({"role": "assistant", "content": assistant})
    messages.append({"role": "user", "content": clip_text(user_text, CURRENT_INPUT_MAX_CHARS)})
    return messages


def estimate_tokens(text):
    if text is None:
        return 0
    cjk = 0
    other = 0
    for char in str(text):
        if char.isspace():
            continue
        if _is_cjk(char):
            cjk += 1
        else:
            other += 1
    return cjk + -(-other // 4)


def _memory_text(item):
    if isinstance(item, dict):
        text = item.get("text")
        return text.strip() if isinstance(text, str) else ""
    if isinstance(item, str):
        return item.strip()
    return ""


def _is_cjk(char):
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FA1F
        or 0x30000 <= code <= 0x323AF
        or code == 0x3007
    )


def _tokens(text):
    normalized = unicodedata.normalize("NFKC", "" if text is None else str(text)).lower()
    tokens = set()
    for run in _runs(normalized):
        if _is_cjk(run[0]):
            if len(run) == 1:
                tokens.add(run)
            else:
                tokens.update(run[index:index + 2] for index in range(len(run) - 1))
        else:
            tokens.add(run)
    return tokens


def _runs(text):
    runs = []
    current = ""
    current_kind = None
    for char in text:
        if _is_cjk(char):
            kind = "cjk"
        elif char.isalnum():
            kind = "word"
        else:
            kind = None
        if kind != current_kind:
            if current:
                runs.append(current)
            current = ""
            current_kind = kind
        if kind is not None:
            current += char
    if current:
        runs.append(current)
    return runs
