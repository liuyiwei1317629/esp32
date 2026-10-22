"""L3 检索基准：500 条 episode、200 次查询，打印 p50/p95。

只用于记录性能，不接主链路、不联网、不占 GPU。
"""

import argparse
import random
import statistics
import time
from datetime import datetime, timedelta, timezone

import memory_score

WORDS = (
    "设计稿", "天气", "香菜", "旅行", "电影", "工作", "妈妈", "豆豆", "咖啡",
    "跑步", "周末", "上海", "北京", "猫", "牛奶", "生日", "会议", "文档",
    "测试", "音乐", "晚饭", "失眠", "朋友", "地铁", "医生", "雨", "书",
    "项目", "论文", "照片", "游戏", "火锅", "早餐", "花", "海边", "手机",
)


def build_episodes(count, seed=7):
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    episodes = []
    kinds = ("event", "near_term", "todo", "preference")
    for index in range(count):
        words = "".join(rng.choice(WORDS) for _ in range(3))
        kind = rng.choice(kinds)
        created = now - timedelta(days=rng.randint(0, 300),
                                  hours=rng.randint(0, 23))
        due = None
        if kind == "todo":
            due = now + timedelta(days=rng.randint(-3, 10))
        episodes.append({
            "id": "e%04d" % index,
            "text": "用户" + words,
            "kind": kind,
            "status": "active",
            "created_at": created.isoformat(timespec="seconds"),
            "updated_at": created.isoformat(timespec="seconds"),
            "event_at": created.isoformat(timespec="seconds"),
            "due_at": due.isoformat(timespec="seconds") if due else None,
            "last_used_at": created.isoformat(timespec="seconds"),
            "hits": rng.randint(0, 8),
        })
    return episodes


def build_queries(count, seed=11):
    rng = random.Random(seed)
    return ["".join(rng.choice(WORDS) for _ in range(2))
            for _ in range(count)]


def percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * ratio))
    return ordered[index]


def run(episodes=500, queries=200, seed=7):
    items = build_episodes(episodes, seed=seed)
    cache = memory_score.TokenCache()
    for item in items:
        cache.tokens(item)
    timings = []
    for query in build_queries(queries, seed=seed + 1):
        start = time.perf_counter_ns()
        memory_score.rank(query, items, k=3, cache=cache)
        timings.append((time.perf_counter_ns() - start) / 1_000_000.0)
    return {
        "episodes": episodes,
        "queries": queries,
        "p50_ms": round(statistics.median(timings), 3),
        "p95_ms": round(percentile(timings, 0.95), 3),
        "max_ms": round(max(timings), 3),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes", type=int, default=500)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    result = run(args.episodes, args.queries, args.seed)
    print("episodes=%d queries=%d p50=%.3f ms p95=%.3f ms max=%.3f ms" % (
        result["episodes"], result["queries"], result["p50_ms"],
        result["p95_ms"], result["max_ms"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
