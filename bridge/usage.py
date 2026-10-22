import json
import os
from datetime import datetime, timezone

import config


def _tokens(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def estimate_cost(usage, price_in_cny_per_mtok, price_out_cny_per_mtok):
    if usage is None:
        return None
    if price_in_cny_per_mtok is None or price_out_cny_per_mtok is None:
        return None
    prompt = _tokens(usage.get("prompt_tokens"))
    completion = _tokens(usage.get("completion_tokens"))
    return round(prompt / 1_000_000 * price_in_cny_per_mtok
                 + completion / 1_000_000 * price_out_cny_per_mtok, 8)


def build_entry(stats, price_in_cny_per_mtok, price_out_cny_per_mtok, now=None):
    profile = stats.get("profile")
    raw_usage = stats.get("usage")
    if not profile or not raw_usage:
        return None
    return {
        "ts": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "profile": profile,
        "prompt_tokens": _tokens(raw_usage.get("prompt_tokens")),
        "completion_tokens": _tokens(raw_usage.get("completion_tokens")),
        "cost_cny": estimate_cost(raw_usage, price_in_cny_per_mtok,
                                  price_out_cny_per_mtok),
        "fallback_used": bool(stats.get("fallback_used")),
    }


def append_entry(entry, path=None):
    path = path or config.USAGE_LEDGER_FILE
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def record_turn(stats, price_lookup=None, path=None, now=None):
    profile = stats.get("profile")
    if not profile or not stats.get("usage"):
        return None
    prices = price_lookup(profile) if price_lookup else (None, None)
    if not prices:
        prices = (None, None)
    entry = build_entry(stats, prices[0], prices[1], now=now)
    if entry is None:
        return None
    append_entry(entry, path)
    return entry
