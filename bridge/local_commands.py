"""Rules-only local replies: no LLM, no network, no TTS, no memory writes.

A timer is parsed, but the request/response bridge cannot speak outside a
turn, so the reply states the limitation instead of promising a reminder
that will never fire.
"""

from datetime import datetime

_STOP_REPLY = "好。"
_TIMER_SUFFIX = "，但现在还不能到点主动出声。"
_DIGITS = "零一二三四五六七八九"


def reply_for(command, now=None):
    if command is None:
        return None
    kind = getattr(command, "kind", "")
    if kind == "stop":
        return _STOP_REPLY
    if kind == "time":
        return format_time_reply(now)
    if kind == "timer" and command.seconds:
        duration = format_duration(command.seconds)
        label = (command.label or "").strip()
        target = "提醒你" + label if label else "提醒你"
        return "我知道你想在%s后%s%s" % (duration, target, _TIMER_SUFFIX)
    return None


def format_time_reply(now=None):
    now = now or datetime.now()
    period, hour = _period_hour(now.hour)
    return "现在是%s%s点%s。" % (
        period, _cn_number(hour), _minute_text(now.minute))


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append("%d小时" % hours)
    if minutes:
        parts.append("%d分钟" % minutes)
    if secs:
        parts.append("%d秒" % secs)
    return "".join(parts) or "0秒"


def _period_hour(hour):
    if hour < 6:
        return "凌晨", hour
    if hour < 12:
        return "上午", hour
    if hour == 12:
        return "中午", 12
    if hour < 18:
        return "下午", hour - 12
    return "晚上", hour - 12


def _minute_text(minute):
    if minute == 0:
        return "整"
    if minute < 10:
        return "零%s分" % _cn_number(minute)
    return "%s分" % _cn_number(minute)


def _cn_number(value):
    if value < 10:
        return _DIGITS[value]
    if value < 20:
        return "十" + (_DIGITS[value % 10] if value % 10 else "")
    return _DIGITS[value // 10] + "十" + (
        _DIGITS[value % 10] if value % 10 else "")
