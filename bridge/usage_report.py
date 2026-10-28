"""用量账本汇总：今日 / 本月 / 累计花了多少钱（只读，不写账本）。"""

import json
import os
from datetime import datetime, timezone

import config

CURRENCY = "CNY"
DIGITS = 6


def _parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _cost(value):
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _new_bucket():
    return {"today_cny": 0.0, "month_cny": 0.0, "total_cny": 0.0,
            "turns": 0, "unpriced_turns": 0}


def _round_bucket(bucket):
    for key in ("today_cny", "month_cny", "total_cny"):
        bucket[key] = round(bucket[key], DIGITS)
    return bucket


def summarize(entries, now=None, tz=None):
    tz = tz or datetime.now().astimezone().tzinfo
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(tz).date()

    totals = _new_bucket()
    by_profile = {}

    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        stamp = _parse_ts(entry.get("ts"))
        profile = entry.get("profile")
        if stamp is None or not profile:
            continue
        local_day = stamp.astimezone(tz).date()
        cost = _cost(entry.get("cost_cny"))

        bucket = by_profile.setdefault(str(profile), _new_bucket())
        for target in (totals, bucket):
            target["turns"] += 1
            if cost is None:
                target["unpriced_turns"] += 1
            else:
                target["total_cny"] += cost
                if (local_day.year, local_day.month) == (today.year, today.month):
                    target["month_cny"] += cost
                if local_day == today:
                    target["today_cny"] += cost

    return {
        "currency": CURRENCY,
        "day": today.isoformat(),
        "month": "%04d-%02d" % (today.year, today.month),
        "today_cny": round(totals["today_cny"], DIGITS),
        "month_cny": round(totals["month_cny"], DIGITS),
        "total_cny": round(totals["total_cny"], DIGITS),
        "turns": totals["turns"],
        "unpriced_turns": totals["unpriced_turns"],
        "by_profile": {name: _round_bucket(bucket)
                       for name, bucket in sorted(by_profile.items())},
        "generated_at": now.astimezone(tz).isoformat(timespec="seconds"),
    }


def load(path=None):
    path = path or config.USAGE_LEDGER_FILE
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows


def report(path=None, now=None, tz=None):
    return summarize(load(path), now=now, tz=tz)
