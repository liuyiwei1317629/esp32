"""Rules-only command parser for the local voice bridge.

``parse_command`` turns one utterance into a :class:`Command`. It only parses:
it never executes commands, starts threads or timers, sleeps, calls TTS, or
touches the network.

Bridge limitation (do not hide this in the integration layer): the current
HTTP contract is request/response. The device starts a turn and only then does
the server return audio, so the server cannot speak outside a turn. A parsed
``timer`` is just data; nobody may pretend that a reminder will fire when it
expires. Real timed reminders need device-side long polling / WebSocket
support or a local buzzer.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    kind: str
    seconds: int | None = None
    label: str = ""
    raw: str = ""


_STOP_PHRASES = frozenset(
    {
        "停",
        "停停",
        "停一下",
        "停一停",
        "停下来",
        "停止",
        "别说了",
        "别讲了",
        "别念了",
        "不要说了",
        "别吵了",
        "闭嘴",
        "安静",
        "安静点",
        "打断",
    }
)

_TIME_PHRASES = frozenset(
    {
        "现在几点",
        "现在几点了",
        "现在几点钟",
        "几点了",
        "几点",
        "现在时间",
        "现在是什么时间",
        "什么时间",
        "报时",
    }
)

# "叫我" is not in the keyword list of the spec, but the spec's own timer
# examples ("半小时后叫我", "一刻钟后叫我") require it to count as a cue.
_TIMER_KEYWORDS = ("提醒", "定时", "计时", "倒计时", "闹钟", "叫我")

_STOP_PARTICLES = ("好吗", "行吗", "吧", "啊", "呀", "呢")
_TIME_PARTICLES = ("了", "啊", "呀", "呢", "吗")

_MAX_STOP_CHARS = 12
_TRAILING_CHARS = "。！？!?~～…，,、. \t\r\n\v\f"
_MIN_SECONDS = 1
_MAX_SECONDS = 86400

_NEGATION_PATTERNS = (
    re.compile(r"(?:别|不要|不用)(?:提醒|定时|计时)"),
    re.compile(r"取消[^，。！？!?]{0,6}(?:提醒|定时|计时|闹钟|倒计时)"),
    re.compile(r"关掉[^，。！？!?]{0,6}(?:闹钟|提醒|计时)"),
    re.compile(r"停止(?:计时|倒计时)"),
)

_UNIT_PATTERN = r"秒钟|秒|分钟|分|小时|钟头"
_UNIT_SECONDS = {"秒": 1, "秒钟": 1, "分": 60, "分钟": 60, "小时": 3600, "钟头": 3600}

_ONE_AND_HALF_RE = re.compile(r"一个半(?:小时|钟头)")
_QUARTER_RE = re.compile(r"一刻钟")
_HALF_RE = re.compile(rf"半\s*(?P<unit>{_UNIT_PATTERN})")
_ARABIC_DURATION_RE = re.compile(rf"(?P<num>\d+(?:\.\d)?)\s*(?P<unit>{_UNIT_PATTERN})")
_CHINESE_DURATION_RE = re.compile(
    rf"(?P<num>[一二两三四五六七八九十]+)\s*(?P<unit>{_UNIT_PATTERN})"
)

_UNSUPPORTED_MAGNITUDE_RE = re.compile(
    rf"[一二两三四五六七八九十][百千万][一二两三四五六七八九十]*\s*(?:{_UNIT_PATTERN})"
)
_UNSUPPORTED_PRECISION_RE = re.compile(rf"\d+\.\d{{2,}}\s*(?:{_UNIT_PATTERN})")

_LABEL_NOISE = (
    "别忘了",
    "别忘记",
    "提醒一下",
    "提醒我",
    "叫我",
    "定个",
    "设个",
    "以后",
    "之后",
    "一下",
    "倒计时",
    "计时",
    "定时",
    "提醒",
    "闹钟",
    "后",
    "的",
)
_LABEL_EDGE_CHARS = (
    " \t\r\n\v\f\u3000"
    "。！？!?~～…，,、；;：:."
    "\"'“”‘’"
    "《》〈〉「」『』【】[]()（）{}"
    "—-·"
)

_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def parse_command(text: str) -> Command | None:
    raw = text.strip()
    if not raw:
        return None

    sentence = _strip_tail(unicodedata.normalize("NFKC", text))
    if not sentence:
        return None

    if _is_stop(sentence):
        return Command(kind="stop", raw=raw)
    if _is_time(sentence):
        return Command(kind="time", raw=raw)
    if any(keyword in sentence for keyword in _TIMER_KEYWORDS):
        parsed = _parse_timer(sentence)
        if parsed is None:
            return None
        seconds, label = parsed
        return Command(kind="timer", seconds=seconds, label=label, raw=raw)
    return None


def _strip_tail(text: str) -> str:
    return text.rstrip(_TRAILING_CHARS).strip()


def _strip_particle(text: str, particles: tuple[str, ...]) -> str:
    for particle in particles:
        if text.endswith(particle):
            return text[: -len(particle)]
    return text


def _is_stop(sentence: str) -> bool:
    if len(sentence) > _MAX_STOP_CHARS:
        return False
    return _strip_particle(sentence, _STOP_PARTICLES) in _STOP_PHRASES


def _is_time(sentence: str) -> bool:
    return _strip_particle(sentence, _TIME_PARTICLES) in _TIME_PHRASES


def _parse_timer(sentence: str) -> tuple[int, str] | None:
    if any(pattern.search(sentence) for pattern in _NEGATION_PATTERNS):
        return None
    if _UNSUPPORTED_MAGNITUDE_RE.search(sentence):
        return None
    if _UNSUPPORTED_PRECISION_RE.search(sentence):
        return None

    candidates: list[tuple[int, int, float | None, str, bool]] = []

    for match in _ONE_AND_HALF_RE.finditer(sentence):
        candidates.append((match.start(), match.end(), 5400.0, match.group(0), True))
    for match in _QUARTER_RE.finditer(sentence):
        candidates.append((match.start(), match.end(), 900.0, match.group(0), True))
    for match in _HALF_RE.finditer(sentence):
        unit = match.group("unit")
        if unit in ("秒", "秒钟"):
            candidates.append((match.start(), match.end(), None, match.group(0), False))
        else:
            candidates.append(
                (match.start(), match.end(), 0.5 * _UNIT_SECONDS[unit], match.group(0), True)
            )
    for match in _ARABIC_DURATION_RE.finditer(sentence):
        value = float(match.group("num")) * _UNIT_SECONDS[match.group("unit")]
        candidates.append((match.start(), match.end(), value, match.group(0), True))
    for match in _CHINESE_DURATION_RE.finditer(sentence):
        number = _chinese_to_int(match.group("num"))
        if number is None:
            candidates.append((match.start(), match.end(), None, match.group(0), False))
        else:
            value = number * _UNIT_SECONDS[match.group("unit")]
            candidates.append((match.start(), match.end(), value, match.group(0), True))

    if any(not candidate[4] for candidate in candidates):
        return None

    candidates.sort(key=lambda candidate: (candidate[0], -(candidate[1] - candidate[0])))
    selected: list[tuple[int, int, float | None, str, bool]] = []
    for candidate in candidates:
        start, end = candidate[0], candidate[1]
        if any(start < chosen[1] and chosen[0] < end for chosen in selected):
            continue
        selected.append(candidate)

    if len(selected) != 1:
        return None

    start, end, value, _, _ = selected[0]
    seconds = int(value + 0.5)
    if seconds < _MIN_SECONDS or seconds > _MAX_SECONDS:
        return None

    label = _extract_label(sentence[:start] + sentence[end:])
    return seconds, label


def _chinese_to_int(text: str) -> int | None:
    if "十" in text:
        if text.count("十") != 1:
            return None
        tens_text, ones_text = text.split("十")
        if tens_text and tens_text not in "一二三四五六七八九":
            return None
        if ones_text and ones_text not in "一二三四五六七八九":
            return None
        tens = _CN_DIGITS[tens_text] if tens_text else 1
        ones = _CN_DIGITS[ones_text] if ones_text else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


def _extract_label(text: str) -> str:
    for noise in _LABEL_NOISE:
        text = text.replace(noise, "")
    label = text.strip(_LABEL_EDGE_CHARS)
    label = re.sub(r"\s+", " ", label).strip(_LABEL_EDGE_CHARS)
    return label[:40]
