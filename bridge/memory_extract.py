"""规则抽取与敏感信息过滤（纯标准库）。

v1 只抽取：
- 显式“记住/记一下/帮我记住/别忘了” -> facts，pinned，source=explicit
- 姓名 / 偏好 / 居住 / 工作 / 学校 / 关系 / 宠物 -> facts 或 episodes
- 待办（带时间或“提醒我”）-> episodes，能解析到期日就填 due_at

一般事件/近况不抽，留给 L2 摘要。敏感值永不自动入库。
所有相对日期以回合发生时的本地时区为锚，解析不了就不填 due_at。
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone

from memory_store import clip_text, normalize_text

MAX_ITEM_CHARS = 30
MAX_OBJECT_CHARS = 20
CONFIDENCE = 0.8
EXPLICIT_CONFIDENCE = 0.95

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
VERIFY_CODE_RE = re.compile(
    r"(?:验证码|校验码|动态码|短信码|otp|one[ -]?time code)"
    r".{0,8}?\d{4,8}|\d{4,8}.{0,8}?(?:验证码|校验码|动态码)",
    re.IGNORECASE,
)
PASSWORD_RE = re.compile(
    r"(?:密码|口令|password|passwd|pwd)\s*(?:是|为|:|：|=)?\s*\S+",
    re.IGNORECASE,
)
API_KEY_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|"
    r"(?:api[_ -]?key|access[_ -]?token|secret|bearer|token)"
    r"\s*(?:[:=：]|是|为)\s*[A-Za-z0-9_./+-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
ADDRESS_RE = re.compile(
    r"(?:住址|地址|住在|家在).{0,30}?"
    r"\d+\s*(?:号|栋|幢|单元|室|楼)|"
    r"(?:路|街|道|巷|弄).{0,20}?\d+\s*(?:号|栋|幢|单元|室|楼)"
)

FILLER_PREFIX_RE = re.compile(
    r"^(?:(?:嗯+|呃+|额+|啊+|哦+|唉+|那个|就是|然后|其实|所以)[，,。.！!？?\s]*)+"
)

EXPLICIT_RES = (
    re.compile(r"(?:请|麻烦|帮我|给我)?记住[，,:：]?\s*(.+)"),
    re.compile(r"记一下[，,:：]?\s*(.+)"),
    re.compile(r"别忘了[，,:：]?\s*(.+)"),
)
NAME_RES = (
    re.compile(r"^我的名字(?:是|叫)[，,:：]?\s*(.+)"),
    re.compile(r"^我叫[，,:：]?\s*(.+)"),
    re.compile(r"^(?:你可以)?叫我[，,:：]?\s*(.+)"),
)
PREFERENCE_RES = (
    re.compile(r"^我(?:很|最|特别|超|挺|有点)?不喜欢(.+)"),
    re.compile(r"^我(?:最|很|特别|超|挺)?讨厌(.+)"),
    re.compile(r"^我(?:最|很|特别|超|挺)?喜欢(.+)"),
    re.compile(r"^我(?:最|特别|超)?爱(.+)"),
)
RESIDENCE_RE = re.compile(r"^我住在[，,:：]?\s*(.+)")
WORK_RE = re.compile(r"^我在(.+?)(?:工作|上班)")
SCHOOL_RE = re.compile(r"^我在(.+?)(?:上学|读书|上大学)")
PET_RE = re.compile(r"^我养了[，,:：]?\s*(.+)")
RELATION_RE = re.compile(
    r"^我的(爸爸|妈妈|父亲|母亲|哥哥|姐姐|弟弟|妹妹|老公|老婆|丈夫|妻子|"
    r"儿子|女儿|爷爷|奶奶|姥爷|姥姥|外公|外婆|朋友)(?:是|叫)[，,:：]?\s*(.+)"
)
TODO_RE = re.compile(
    r"^我(?P<when>今天|明天|后天|大后天|"
    r"下下周[一二三四五六日天]?|下周[一二三四五六日天]?)?"
    r"(?P<verb>要|得|需要|准备|计划)(?P<action>.+)"
)
REMIND_RE = re.compile(
    r"^(?<!别)(?<!不要)(?<!不用)(?:请|麻烦)?提醒我[，,:：]?\s*(.+)")
RELATIVE_RE = re.compile(
    r"^(今天|明天|后天|大后天|下下周[一二三四五六日天]?|"
    r"下周[一二三四五六日天]?|\d{1,2}月\d{1,2}[日号]?)"
)
INTERROGATIVE_RE = re.compile(r"(什么|啥|谁|哪|怎么|为什么|多少|是否|多久)")
THIRD_PERSON_RE = re.compile(r"^(?:他|她|他们|她们|别人|朋友|同事)")
REPORTED_RE = re.compile(r"(?:他说|她说|他们说|她们说|朋友说|同事说|别人说)")
BAD_NAME_RE = re.compile(r"^(?:什么|啥|谁|哪个|名字|一下|我|你|他|她)$")


def strip_fillers(text):
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).strip()
    return FILLER_PREFIX_RE.sub("", value).strip()


def _clean_capture(value, limit=MAX_OBJECT_CHARS):
    value = strip_fillers(value)
    value = re.split(r"[。！？!?；;，,\n]", value, maxsplit=1)[0]
    value = value.strip(" \t，,。.！!？?、：:")
    return clip_text(value, limit)


def is_question(text):
    value = (text or "").strip()
    if not value:
        return False
    if value.endswith(("?", "？")):
        return True
    if re.search(r"(?:吗|呢|么|嘛)[。.!！?？]?$", value):
        return True
    # “我喜欢什么”这类没有问号的疑问句；带“什么”但后面有实际内容的
    # （“我明天要买什么礼物”）也先不抽，宁可漏记。
    return bool(INTERROGATIVE_RE.search(value))


def is_hypothetical(text):
    return bool(re.search(r"(如果|要是|假如|万一|若是|假设)", text or ""))


def is_third_person(text):
    value = (text or "").strip()
    return bool(THIRD_PERSON_RE.search(value) or REPORTED_RE.search(value))


# ---------------------------------------------------------------------------
# 敏感信息
# ---------------------------------------------------------------------------
def sensitive_reasons(text):
    value = "" if text is None else str(text)
    reasons = []
    if PHONE_RE.search(value):
        reasons.append("phone")
    if ID_CARD_RE.search(value):
        reasons.append("id_card")
    if BANK_CARD_RE.search(value):
        reasons.append("bank_card")
    if VERIFY_CODE_RE.search(value):
        reasons.append("verification_code")
    if PASSWORD_RE.search(value):
        reasons.append("password")
    if API_KEY_RE.search(value):
        reasons.append("api_key")
    if ADDRESS_RE.search(value):
        reasons.append("address")
    return reasons


def has_sensitive(text):
    return bool(sensitive_reasons(text))


# ---------------------------------------------------------------------------
# 日期
# ---------------------------------------------------------------------------
def _local_now(now=None):
    if now is None:
        return datetime.now().astimezone()
    if isinstance(now, str):
        raw = now.strip().replace("Z", "+00:00")
        now = datetime.fromisoformat(raw)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.now().astimezone().tzinfo)
    if now.tzinfo == timezone.utc:
        return now.astimezone()
    # 调用方传入的 tzinfo 视为回合发生地的本地时区，保证测试可复现。
    return now


def resolve_due(phrase, now=None):
    """把“今天/明天/后天/下周X/N月N日”解析成 UTC 的 00:00。

    解析不了返回 None；不猜。
    """
    text = (phrase or "").strip()
    if not text:
        return None
    local = _local_now(now)
    match = RELATIVE_RE.match(text)
    if not match:
        return None
    rest = text[match.end():].strip(" ，,。.！!？?")
    if rest and not rest.startswith(
            ("上午", "下午", "晚上", "早上", "中午", "凌晨", "今晚", "明早")):
        return None
    token = match.group(1)
    base = local.date()
    due_date = None
    day_offsets = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
    if token in day_offsets:
        due_date = base + timedelta(days=day_offsets[token])
    elif token.startswith("下下周") or token.startswith("下周"):
        weeks = 2 if token.startswith("下下周") else 1
        suffix = token[3:] if weeks == 2 else token[2:]
        days_to_monday = 7 - base.weekday()
        monday = base + timedelta(days=days_to_monday + 7 * (weeks - 1))
        if suffix:
            offset = "一二三四五六日天".find(suffix[0])
            if offset < 0:
                return None
            if offset == 6:
                offset = 6
            monday = monday + timedelta(days=offset)
        due_date = monday
    else:
        month_day = re.match(r"(\d{1,2})月(\d{1,2})[日号]?", token)
        if not month_day:
            return None
        month = int(month_day.group(1))
        day = int(month_day.group(2))
        try:
            due_date = base.replace(year=base.year, month=month, day=day)
        except ValueError:
            return None
    local_due = datetime(due_date.year, due_date.month, due_date.day,
                         tzinfo=local.tzinfo)
    return local_due.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------
def _candidate(kind, text, slot=None, confidence=CONFIDENCE, **extra):
    item = {
        "kind": kind,
        "text": clip_text(text, MAX_ITEM_CHARS),
        "slot": slot,
        "confidence": confidence,
        "source": "rule",
    }
    item.update(extra)
    return item


def _first_match(patterns, text):
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match
    return None


def _explicit(text, now):
    match = _first_match(EXPLICIT_RES, text)
    if not match:
        return None
    content = _clean_capture(match.group(1), MAX_ITEM_CHARS)
    if not content:
        return None
    content = re.sub(r"^我的", "用户的", content)
    content = re.sub(r"^我", "用户", content)
    return _candidate("fact", content, None, EXPLICIT_CONFIDENCE,
                      pinned=True, source="explicit")


def _name(text):
    match = _first_match(NAME_RES, text)
    if not match:
        return None
    name = _clean_capture(match.group(1), 12)
    if not name or BAD_NAME_RE.match(name):
        return None
    return _candidate("name", "用户叫" + name, "name")


def _preference(text):
    match = _first_match(PREFERENCE_RES, text)
    if not match:
        return None
    pattern = match.re.pattern
    value = _clean_capture(match.group(1), MAX_OBJECT_CHARS)
    if not value or INTERROGATIVE_RE.search(value) or BAD_NAME_RE.match(value):
        return None
    if "不喜欢" in pattern:
        prefix = "用户不喜欢"
        polarity = "negative"
    elif "讨厌" in pattern:
        prefix = "用户不喜欢"
        polarity = "negative"
    else:
        prefix = "用户喜欢"
        polarity = "positive"
    slot = "pref:" + clip_text(normalize_text(value), 18)
    return _candidate("preference", prefix + value, slot,
                      polarity=polarity)


def _residence(text):
    match = RESIDENCE_RE.search(text)
    if not match:
        return None
    place = _clean_capture(match.group(1), MAX_OBJECT_CHARS)
    if not place or INTERROGATIVE_RE.search(place):
        return None
    return _candidate("residence", "用户住在" + place, "city")


def _work_or_school(text):
    match = WORK_RE.search(text)
    if match:
        place = _clean_capture(match.group(1), MAX_OBJECT_CHARS)
        if place and not INTERROGATIVE_RE.search(place):
            return _candidate("fact", "用户在" + place + "工作", "work")
    match = SCHOOL_RE.search(text)
    if match:
        place = _clean_capture(match.group(1), MAX_OBJECT_CHARS)
        if place and not INTERROGATIVE_RE.search(place):
            return _candidate("fact", "用户在" + place + "上学", "school")
    return None


def _pet(text):
    match = PET_RE.search(text)
    if not match:
        return None
    value = _clean_capture(match.group(1), MAX_OBJECT_CHARS)
    if not value or INTERROGATIVE_RE.search(value):
        return None
    named = re.search(r"叫([\w\u4e00-\u9fa5]{1,10})", value)
    slot = "pet:" + (named.group(1) if named else clip_text(normalize_text(value), 12))
    return _candidate("relation", "用户养了" + value, slot)


def _relation(text):
    match = RELATION_RE.search(text)
    if not match:
        return None
    relation = match.group(1)
    name = _clean_capture(match.group(2), 12)
    if not name or BAD_NAME_RE.match(name):
        return None
    return _candidate("relation", "用户的" + relation + "是" + name,
                      "relation:" + relation)


def _todo(text, now):
    match = TODO_RE.search(text)
    when = ""
    action = ""
    if match:
        when = match.group("when") or ""
        action = _clean_capture(match.group("action"), MAX_OBJECT_CHARS)
        if not when:
            # “我要…”太宽，v1 只收带时间或“提醒我”的待办。
            match = None
    if not match:
        remind = REMIND_RE.search(text)
        if not remind:
            return None
        action = _clean_capture(remind.group(1), MAX_OBJECT_CHARS)
        due_match = RELATIVE_RE.match(action)
        if due_match:
            when = due_match.group(1)
            action = action[len(when):].lstrip("，,。. ")
        if not action:
            return None
    if not action or INTERROGATIVE_RE.search(action):
        return None
    due_at = resolve_due(when, now=now) if when else None
    spoken = ("用户" + when + "要" + action) if when else ("用户要" + action)
    slot = "todo:" + clip_text(normalize_text(action), 18)
    return _candidate("todo", spoken, slot, due_at=due_at,
                      when=when or None)


def extract(text, now=None):
    """返回候选列表；每个候选含 kind/text/slot/confidence/source。"""
    source = strip_fillers(text)
    if not source or has_sensitive(source):
        return []
    if is_question(source) or is_hypothetical(source) or is_third_person(source):
        return []

    explicit = _explicit(source, now)
    if explicit is not None:
        return [explicit]

    candidates = []
    clauses = [part.strip() for part in re.split(r"[。！？!?；;，,\n]", source)
               if part.strip()]
    for clause in clauses or [source]:
        for extractor in (_name, _preference, _residence, _work_or_school,
                          _pet, _relation):
            candidate = extractor(clause)
            if candidate is not None:
                candidates.append(candidate)
        todo = _todo(clause, now)
        if todo is not None:
            candidates.append(todo)

    deduped = []
    seen = set()
    for candidate in candidates:
        key = (candidate["kind"], candidate.get("slot"),
               normalize_text(candidate["text"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


# ---------------------------------------------------------------------------
# 摘要辅助
# ---------------------------------------------------------------------------
def first_sentence(text, limit=28):
    """回复第一个句号/问号/感叹号前的文本；没有标点则取第一行。"""
    value = "" if text is None else str(text).strip()
    if not value:
        return ""
    match = re.search(r"[。！？!?\n]", value)
    if match:
        value = value[:match.start()]
    return clip_text(value, limit)


def clean_intent(text, limit=28):
    """去口头词后的 ASR 文本，用于 L2 最新句。"""
    value = strip_fillers(text)
    value = re.sub(r"[\r\n\t]+", " ", value)
    return clip_text(value, limit)
