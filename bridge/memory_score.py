"""L3 相关往事检索：分词、相似度、时间衰减、待办加权、top-k。

与 B（context_builder）使用同一套中文 bigram + 英文/数字词切分，但本模块
自带公开 tokenizer，不调用 B 的私有 `_tokens`。纯标准库、不联网。
"""

import math
import unicodedata
from datetime import datetime, timezone

DEFAULT_TOP_K = 3
EVENT_TAU_DAYS = 7.0
PREFERENCE_TAU_DAYS = 30.0
VISIT_CAP = 0.5
DUE_SOON_SECONDS = 24 * 3600
DUE_LINEAR_DAYS = 7.0
NO_DUE_SCORE = 0.3


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


def tokenize(text):
    """NFKC + 小写；CJK 取 bigram，英文/数字连续串取一个词。"""
    normalized = unicodedata.normalize("NFKC", "" if text is None else str(text)).lower()
    tokens = set()
    current = ""
    current_kind = None
    for char in normalized:
        if _is_cjk(char):
            kind = "cjk"
        elif char.isalnum():
            kind = "word"
        else:
            kind = None
        if kind != current_kind:
            if current:
                _add_run(tokens, current, current_kind)
            current = ""
            current_kind = kind
        if kind is not None:
            current += char
    if current:
        _add_run(tokens, current, current_kind)
    return tokens


def _add_run(tokens, run, kind):
    if kind == "cjk":
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[index:index + 2] for index in range(len(run) - 1))
    else:
        tokens.add(run)


def similarity(query, text):
    """overlap / max(1, min(|q|, |e|))；query 可以是字符串或 token 集。"""
    query_tokens = query if isinstance(query, (set, frozenset)) else tokenize(query)
    text_tokens = text if isinstance(text, (set, frozenset)) else tokenize(text)
    if not query_tokens or not text_tokens:
        return 0.0
    overlap = len(query_tokens & text_tokens)
    return overlap / max(1, min(len(query_tokens), len(text_tokens)))


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _now(now=None):
    return _as_utc(now) or datetime.now(timezone.utc)


def decay(timestamp, now=None, tau_days=EVENT_TAU_DAYS):
    ts = _as_utc(timestamp)
    if ts is None or tau_days <= 0:
        return 0.0
    age_days = max(0.0, (_now(now) - ts).total_seconds() / 86400.0)
    return math.exp(-age_days / tau_days)


def due_score(due_at, now=None):
    """待办时间权重。

    已过或 24 h 内 = 1.0；超过 24 h 后按 1 - 剩余天数/7 线性降到 0；
    无截止日 = 0.3。解析失败按无截止日处理。
    """
    due = _as_utc(due_at)
    if due is None:
        return NO_DUE_SCORE
    remaining = (due - _now(now)).total_seconds()
    if remaining <= DUE_SOON_SECONDS:
        return 1.0
    return max(0.0, 1.0 - (remaining / 86400.0) / DUE_LINEAR_DAYS)


def _event_timestamp(episode):
    kind = episode.get("kind")
    if kind == "preference":
        return (episode.get("updated_at")
                or episode.get("created_at")
                or episode.get("event_at"))
    return (episode.get("event_at")
            or episode.get("created_at")
            or episode.get("updated_at"))


def _freshness(episode):
    ts = _as_utc(_event_timestamp(episode))
    return ts.timestamp() if ts is not None else float("-inf")


def score_episode(query, episode, now=None, query_tokens=None,
                  text_tokens=None):
    """返回单条 episode 的打分详情（不修改记录）。"""
    if not isinstance(episode, dict):
        raise TypeError("episode 必须是 dict")
    text = episode.get("text") or ""
    tokens = text_tokens if text_tokens is not None else tokenize(text)
    query_set = query_tokens if query_tokens is not None else tokenize(query)
    sim = similarity(query_set, tokens)
    kind = episode.get("kind")
    if kind == "todo":
        rec = due_score(episode.get("due_at"), now=now)
    else:
        tau = PREFERENCE_TAU_DAYS if kind == "preference" else EVENT_TAU_DAYS
        rec = decay(_event_timestamp(episode), now=now, tau_days=tau)
    visit = min(VISIT_CAP, 0.1 * int(episode.get("hits") or 0))
    total = 0.6 * sim + 0.3 * rec + 0.1 * visit
    return {
        "episode": episode,
        "score": total,
        "sim": sim,
        "rec": rec,
        "visit": visit,
        "due": rec if kind == "todo" else None,
    }


def _eligible(entry):
    if entry["sim"] > 0:
        return True
    episode = entry["episode"]
    return episode.get("kind") == "todo" and entry.get("due") == 1.0


def rank(query, episodes, k=DEFAULT_TOP_K, now=None, cache=None):
    """返回按分数降序的 top-k；同分取更新的记录。"""
    now_value = _now(now)
    query_tokens = tokenize(query)
    ranked = []
    for index, episode in enumerate(episodes or ()):
        if not isinstance(episode, dict):
            continue
        if episode.get("status") not in (None, "active"):
            continue
        text = episode.get("text") or ""
        text_tokens = cache.tokens(episode) if cache is not None else tokenize(text)
        entry = score_episode(
            query, episode, now=now_value, query_tokens=query_tokens,
            text_tokens=text_tokens,
        )
        if not _eligible(entry):
            continue
        entry["index"] = index
        ranked.append(entry)
    ranked.sort(key=lambda item: (
        -item["score"],
        -_freshness(item["episode"]),
        item["index"],
    ))
    if k is None or k < 0:
        k = 0
    return ranked[:k]


def top_records(query, episodes, k=DEFAULT_TOP_K, now=None, cache=None):
    return [entry["episode"] for entry in rank(query, episodes, k=k, now=now,
                                               cache=cache)]


class TokenCache:
    """按 episode id 缓存 token 集；文本或 mtime 变化时由调用方失效。"""

    def __init__(self):
        self._cache = {}

    def tokens(self, episode):
        if not isinstance(episode, dict):
            return frozenset()
        key = episode.get("id") or ("text:" + str(episode.get("text") or ""))
        text = episode.get("text") or ""
        cached = self._cache.get(key)
        if cached is not None and cached[0] == text:
            return cached[1]
        tokens = frozenset(tokenize(text))
        self._cache[key] = (text, tokens)
        return tokens

    def invalidate(self, episode_or_id):
        if isinstance(episode_or_id, dict):
            keys = [episode_or_id.get("id")]
            if not keys[0]:
                keys.append("text:" + str(episode_or_id.get("text") or ""))
        else:
            keys = [episode_or_id]
        for key in keys:
            self._cache.pop(key, None)
        return True

    def clear(self):
        self._cache.clear()

    def __len__(self):
        return len(self._cache)
