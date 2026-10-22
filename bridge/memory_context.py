"""五层记忆的上下文组装与硬预算裁剪（纯标准库）。

消息格式与 B（context_builder）一致：
    system: persona
            长期记忆：
            - ...
            之前聊到：...
    user/assistant: 最近 2 轮
    user: 本轮输入

预算按最终 messages 的 estimate_tokens 计算；本轮输入默认最多 500 字
（可配置），不会因为总预算吃紧被静默截成 120。L0 与本轮输入永不参与硬裁。
"""

import re
from datetime import datetime, timezone

import context_builder
import memory_extract
import memory_score

PERSONA_MAX_CHARS = 120
L1_MAX_ITEMS = 2
L1_ITEM_CHARS = 30
L1_MAX_ITEMS_TRIMMED = 1
L2_INJECT_MAX_CHARS = 80
L2_INJECT_STEPS = (80, 60, 40, 0)
SUMMARY_MAX_CHARS = 96
SUMMARY_TOTAL_CHARS = 100
STICKY_MAX_CHARS = 20
L3_MAX_ITEMS = 3
L3_ITEM_CHARS = 30
L3_ITEM_STEPS = (3, 2, 1, 0)
HISTORY_TURNS = 2
HISTORY_USER_CHARS = 40
HISTORY_ASSISTANT_CHARS = 50
HISTORY_USER_TRIMMED = 20
HISTORY_ASSISTANT_TRIMMED = 30
CURRENT_INPUT_MAX_CHARS = 500
TOTAL_BUDGET_TOKENS = 1050

MEMORY_HEADER = "长期记忆："
SUMMARY_HEADER = "之前聊到："
SUMMARY_JOINER = "；"
INJECT_JOINER = "｜"


def _clip(value, limit):
    return context_builder.clip_text(value, limit)


def _clean_part(value):
    value = "" if value is None else str(value)
    value = value.replace(SUMMARY_JOINER, "，").replace(INJECT_JOINER, "，")
    return value.strip()


def _join_summary(intent, reply, old):
    return SUMMARY_JOINER.join(part for part in (intent, reply, old) if part)


def build_summary_storage(intent, reply_first, old_summary, sticky_goal=""):
    """生成 L2 存储值：summary 最新在前，summary+sticky ≤100。

    返回 {"summary": ..., "sticky_goal": ...}。旧摘要先裁 38→20→0；
    仍超才压回复 28→18、意图 28→18；sticky 最后压 20→16。
    """
    intent = _clean_part(memory_extract.clean_intent(intent, 28))
    reply = _clean_part(memory_extract.first_sentence(reply_first, 28))
    old = _clean_part(old_summary)
    sticky = _clip(_clean_part(sticky_goal), STICKY_MAX_CHARS)

    plans = (
        (38, 28, 28, 20),
        (20, 28, 28, 20),
        (0, 28, 28, 20),
        (0, 18, 28, 20),
        (0, 18, 18, 20),
        (0, 18, 18, 16),
    )
    for old_limit, reply_limit, intent_limit, sticky_limit in plans:
        candidate_sticky = _clip(sticky, sticky_limit)
        candidate = _join_summary(
            _clip(intent, intent_limit),
            _clip(reply, reply_limit),
            _clip(old, old_limit),
        )
        if len(candidate) <= SUMMARY_MAX_CHARS and \
                len(candidate) + len(candidate_sticky) <= SUMMARY_TOTAL_CHARS:
            return {"summary": candidate, "sticky_goal": candidate_sticky}
    # 理论上到不了；保底只保留最新意图的前缀。
    candidate = _clip(intent, 18)
    return {"summary": candidate, "sticky_goal": ""}


def _split_summary(summary):
    parts = [part.strip() for part in str(summary or "").split(SUMMARY_JOINER)
             if part.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], SUMMARY_JOINER.join(parts[2:])
    if len(parts) == 2:
        return parts[0], parts[1], ""
    if len(parts) == 1:
        return "", "", parts[0]
    return "", "", ""


def build_summary_injection(summary, sticky_goal="", limit=L2_INJECT_MAX_CHARS):
    """构造 L2 注入串：sticky｜本轮意图｜回复首句｜旧摘要，≤80 字。

    旧摘要先掉（38→20→0），再压回复/意图，sticky 最后；最新句不会被截掉。
    """
    intent, reply, old = _split_summary(summary)
    sticky = _clip(_clean_part(sticky_goal), STICKY_MAX_CHARS)
    plans = (
        (38, 28, 28, 20),
        (20, 28, 28, 20),
        (0, 28, 28, 20),
        (0, 18, 28, 20),
        (0, 18, 18, 20),
        (0, 18, 18, 16),
    )
    for old_limit, reply_limit, intent_limit, sticky_limit in plans:
        parts = [
            _clip(sticky, sticky_limit),
            _clip(intent, intent_limit),
            _clip(reply, reply_limit),
            _clip(old, old_limit),
        ]
        candidate = INJECT_JOINER.join(part for part in parts if part)
        if limit is None or limit <= 0:
            return ""
        if len(candidate) <= limit:
            return candidate
    # 理论上到不了；仍只从尾部（旧摘要）截。
    return context_builder.clip_text(candidate, limit)


# ---------------------------------------------------------------------------
# 最近两轮
# ---------------------------------------------------------------------------
def _turn_ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _turn_key(turn):
    return (str(turn.get("ts") or ""),
            str(turn.get("user") or ""), str(turn.get("assistant") or ""))


def _sanitize_turn(turn, user_limit=HISTORY_USER_CHARS,
                   assistant_limit=HISTORY_ASSISTANT_CHARS):
    if not isinstance(turn, dict):
        return None
    user = "" if turn.get("user") is None else str(turn.get("user")).strip()
    assistant = "" if turn.get("assistant") is None else str(turn.get("assistant")).strip()
    if not user or not assistant:
        return None
    return {
        "user": _clip(user, user_limit),
        "assistant": _clip(assistant, assistant_limit),
        "ts": turn.get("ts"),
    }


def merge_recent_turns(disk_turns=None, cache_turns=None, limit=HISTORY_TURNS):
    """按 ts + user + assistant 去重合并最近的 disk 与 cache 轮次；cache 覆盖同键。"""
    if limit <= 0:
        return []
    merged = {}
    order = []
    for source in (disk_turns or (), cache_turns or ()):
        for turn in source:
            clean = _sanitize_turn(turn)
            if clean is None:
                continue
            key = _turn_key(clean)
            if key not in merged:
                order.append(key)
            merged[key] = clean
    items = [merged[key] for key in order]
    parsed = [_turn_ts(item.get("ts")) for item in items]
    if items and all(value is not None for value in parsed):
        pairs = sorted(zip(parsed, items), key=lambda pair: pair[0])
        items = [item for _ts, item in pairs]
    return items[-limit:]


# ---------------------------------------------------------------------------
# 五层组装
# ---------------------------------------------------------------------------
def _fact_sort_key(item):
    used = _turn_ts(item.get("last_used_at"))
    used_ts = used.timestamp() if used else float("-inf")
    return (
        0 if item.get("pinned") else 1,
        -used_ts,
        -int(item.get("hits") or 0),
        str(item.get("id") or ""),
    )


def _active(records):
    result = []
    for item in records or ():
        if not isinstance(item, dict):
            continue
        if item.get("status") not in (None, "active"):
            continue
        if not str(item.get("text") or "").strip():
            continue
        result.append(item)
    return result


def _prepare_episodes(episodes):
    raw = []
    for item in episodes or ():
        if isinstance(item, dict) and isinstance(item.get("episode"), dict):
            raw.append(item["episode"])
        elif isinstance(item, dict):
            raw.append(item)
    return raw


def _history_for_attempt(history, turns, user_limit, assistant_limit):
    if turns <= 0:
        return []
    items = []
    for turn in history[-turns:]:
        clean = _sanitize_turn(turn, user_limit, assistant_limit)
        if clean is not None:
            items.append(clean)
    return items


def _assemble(persona, facts, l1_limit, summary, sticky, episodes,
              l3_limit, history, current_input, l2_limit=L2_INJECT_MAX_CHARS):
    parts = [_clip(persona, PERSONA_MAX_CHARS)]
    memory_lines = []
    used_facts = []
    for fact in facts[:l1_limit]:
        text = _clip(fact.get("text"), L1_ITEM_CHARS)
        if text:
            memory_lines.append("- " + text)
            used_facts.append(fact)
    used_episodes = []
    for episode in episodes[:l3_limit]:
        text = _clip(episode.get("text"), L3_ITEM_CHARS)
        line = "- " + text
        if text and line not in memory_lines:
            memory_lines.append(line)
            used_episodes.append(episode)
    if memory_lines:
        parts.append(MEMORY_HEADER + "\n" + "\n".join(memory_lines))

    l2_text = build_summary_injection(summary, sticky, l2_limit)
    if l2_text:
        parts.append(SUMMARY_HEADER + l2_text)

    messages = [{"role": "system", "content": "\n\n".join(parts)}]
    for turn in history:
        messages.append({"role": "user", "content": turn["user"]})
        messages.append({"role": "assistant", "content": turn["assistant"]})
    messages.append({"role": "user", "content": current_input})
    return messages, l2_text, used_facts, used_episodes


def _token_total(messages):
    return sum(context_builder.estimate_tokens(item.get("content"))
               for item in messages)


def build_messages(persona, facts, summary, sticky_goal, episodes,
                   recent_turns, user_text, budget_tokens=TOTAL_BUDGET_TOKENS,
                   now=None, token_cache=None,
                   current_input_max_chars=CURRENT_INPUT_MAX_CHARS):
    """返回 (messages, info)。异常由调用方回退，不在这里吞掉。"""
    persona = "" if persona is None else str(persona).strip()
    user_text = "" if user_text is None else str(user_text).strip()
    if not persona:
        raise ValueError("persona 不能为空")
    if not user_text:
        raise ValueError("user_text 不能为空")

    fact_items = sorted(_active(facts), key=_fact_sort_key)
    pinned_count = sum(1 for item in fact_items if item.get("pinned"))
    pinned_overflow = pinned_count > L1_MAX_ITEMS

    episode_items = _prepare_episodes(episodes)
    ranked = memory_score.rank(user_text, episode_items, k=L3_MAX_ITEMS,
                               now=now, cache=token_cache)
    episode_items = [entry["episode"] for entry in ranked]
    l3_scores = [{"id": entry["episode"].get("id"),
                  "score": round(entry["score"], 6),
                  "sim": round(entry["sim"], 6)} for entry in ranked]

    history = merge_recent_turns(recent_turns, (), HISTORY_TURNS)
    current = _clip(user_text, current_input_max_chars)

    configs = (
        (2, 80, 3, 2, 40, 50),
        (2, 60, 3, 2, 40, 50),
        (2, 40, 3, 2, 40, 50),
        (2, 40, 2, 2, 40, 50),
        (2, 40, 1, 2, 40, 50),
        (2, 40, 1, 1, 40, 50),
        (2, 40, 1, 1, 20, 30),
        (1, 40, 1, 1, 20, 30),
        (1, 0, 1, 1, 20, 30),
        (1, 0, 0, 1, 20, 30),
        (1, 0, 0, 0, 20, 30),
    )
    labels = (
        "",
        "L2:60",
        "L2:40",
        "L3:2",
        "L3:1",
        "L4:drop_oldest",
        "L4:clip",
        "L1:1",
        "L2:0",
        "L3:0",
        "L4:0",
    )

    chosen = None
    for index, (l1_limit, l2_limit, l3_limit, turns,
                user_limit, assistant_limit) in enumerate(configs):
        attempt_history = _history_for_attempt(history, turns, user_limit,
                                               assistant_limit)
        if l2_limit == 0:
            attempt_summary, attempt_sticky = "", ""
        else:
            attempt_summary, attempt_sticky = summary, sticky_goal
        messages, l2_text, used_facts, used_episodes = _assemble(
            persona, fact_items, l1_limit, attempt_summary, attempt_sticky,
            episode_items, l3_limit, attempt_history, current,
            l2_limit=l2_limit,
        )
        total = _token_total(messages)
        if total <= budget_tokens:
            chosen = (index, messages, l2_text, total, attempt_history,
                      used_facts, used_episodes)
            break
    if chosen is None:
        # L0 与本轮输入不砍；退到最小层组合，仍超预算则告警。
        index = len(configs) - 1
        _l1, _l2, _l3, turns, user_limit, assistant_limit = configs[index]
        attempt_history = _history_for_attempt(history, turns, user_limit,
                                               assistant_limit)
        messages, l2_text, used_facts, used_episodes = _assemble(
            persona, fact_items, 1, "", "", episode_items, 0,
            attempt_history, current,
        )
        chosen = (index, messages, l2_text, _token_total(messages),
                  attempt_history, used_facts, used_episodes)

    index, messages, l2_text, total, attempt_history, used_facts, \
        used_episodes = chosen
    trimmed = [label for label in labels[1:index + 1] if label]
    warnings = []
    if pinned_overflow:
        warnings.append("pinned_overflow")
    if total > budget_tokens:
        warnings.append("over_budget")
    if budget_tokens < TOTAL_BUDGET_TOKENS:
        warnings.append("low_budget")

    selected_facts = used_facts
    selected_episodes = used_episodes
    info = {
        "budget_tokens": budget_tokens,
        "total_tokens": total,
        "l0": 1 if persona else 0,
        "l1": len(selected_facts),
        "l2_chars": len(l2_text),
        "l3": len(selected_episodes),
        "l4_turns": len(attempt_history),
        "current_chars": len(current),
        "trimmed": trimmed,
        "warnings": warnings,
        "pinned_overflow": pinned_overflow,
        "pinned_count": pinned_count,
        "selected_ids": [item.get("id") for item in selected_facts]
                        + [item.get("id") for item in selected_episodes],
        "l3_scores": l3_scores,
        "low_budget": budget_tokens < TOTAL_BUDGET_TOKENS,
    }
    return messages, info


def fallback_messages(persona, user_text,
                      current_input_max_chars=CURRENT_INPUT_MAX_CHARS):
    persona = _clip(persona, PERSONA_MAX_CHARS)
    user_text = _clip(user_text, current_input_max_chars)
    messages = []
    if persona:
        messages.append({"role": "system", "content": persona})
    messages.append({"role": "user", "content": user_text})
    return messages


def format_debug(info):
    """单行计数；只输出层计数与预算，绝不输出记忆或用户原文。"""
    if info.get("fallback"):
        return "memory: fallback=True error=%s" % (info.get("error") or "-")
    trimmed = ",".join(info.get("trimmed") or ()) or "-"
    warnings = ",".join(info.get("warnings") or ()) or "-"
    return ("memory: L1=%d L2=%d L3=%d L4=%d total=%d/%d "
            "trimmed=%s current=%d warnings=%s"
            % (int(info.get("l1") or 0), int(info.get("l2_chars") or 0),
               int(info.get("l3") or 0), int(info.get("l4_turns") or 0),
               int(info.get("total_tokens") or 0),
               int(info.get("budget_tokens") or 0), trimmed,
               int(info.get("current_chars") or 0), warnings))


def build_for_turn(store, persona, session_id, user_text,
                   budget_tokens=TOTAL_BUDGET_TOKENS, recent_cache=None,
                   token_cache=None, now=None,
                   current_input_max_chars=CURRENT_INPUT_MAX_CHARS):
    """读 store 并组装；任何异常回退到 persona + 本轮输入。"""
    try:
        facts = store.active_facts()
        episodes = store.active_episodes()
        session = store.load_session(session_id)
        recent = merge_recent_turns(session.get("recent_turns"), recent_cache,
                                    HISTORY_TURNS)
        messages, info = build_messages(
            persona, facts, session.get("summary", ""),
            session.get("sticky_goal", ""), episodes, recent, user_text,
            budget_tokens=budget_tokens, now=now, token_cache=token_cache,
            current_input_max_chars=current_input_max_chars,
        )
        info["session_id"] = session_id
        info["fallback"] = False
        return messages, info
    except Exception as exc:
        info = {
            "session_id": session_id,
            "fallback": True,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "budget_tokens": budget_tokens,
        }
        return fallback_messages(persona, user_text,
                                 current_input_max_chars), info
