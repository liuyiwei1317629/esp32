import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_score

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 23, 1, 0, tzinfo=CST)


def episode(text, **kwargs):
    item = {
        "id": str(abs(hash((text, tuple(sorted(kwargs.items()))))) ),
        "text": text,
        "kind": "event",
        "status": "active",
        "created_at": NOW.isoformat(timespec="seconds"),
        "updated_at": NOW.isoformat(timespec="seconds"),
        "event_at": None,
        "due_at": None,
        "last_used_at": None,
        "hits": 0,
    }
    item.update(kwargs)
    return item


class TokenizeTest(unittest.TestCase):
    def test_cjk_bigrams(self):
        self.assertEqual(memory_score.tokenize("今天天气"),
                         {"今天", "天天", "天气"})

    def test_single_cjk_char(self):
        self.assertEqual(memory_score.tokenize("雨"), {"雨"})

    def test_english_and_numbers_are_runs(self):
        self.assertEqual(memory_score.tokenize("Python 3.13"),
                         {"python", "3", "13"})

    def test_width_and_case_are_normalized(self):
        self.assertEqual(memory_score.tokenize("ＰＹＴＨＯＮ"), {"python"})


class SimilarityTest(unittest.TestCase):
    def test_full_overlap_is_one(self):
        self.assertEqual(memory_score.similarity("今天天气", "今天天气很好"), 1.0)

    def test_empty_is_zero(self):
        self.assertEqual(memory_score.similarity("", "今天天气"), 0.0)
        self.assertEqual(memory_score.similarity("今天", ""), 0.0)

    def test_no_overlap_is_zero(self):
        self.assertEqual(memory_score.similarity("天气", "完全无关"), 0.0)

    def test_accepts_token_sets(self):
        query = {"今天", "天气"}
        self.assertEqual(memory_score.similarity(query, "今天天气"), 1.0)


class DecayTest(unittest.TestCase):
    def test_event_decay_at_one_tau(self):
        old = NOW - timedelta(days=7)
        self.assertAlmostEqual(
            memory_score.decay(old, now=NOW, tau_days=7), math.exp(-1))

    def test_future_timestamp_is_fresh(self):
        future = NOW + timedelta(days=3)
        self.assertAlmostEqual(memory_score.decay(future, now=NOW), 1.0)

    def test_missing_or_zero_tau_is_zero(self):
        self.assertEqual(memory_score.decay(None, now=NOW), 0.0)
        self.assertEqual(memory_score.decay(NOW, now=NOW, tau_days=0), 0.0)


class DueScoreTest(unittest.TestCase):
    def test_no_due_is_point_three(self):
        self.assertEqual(memory_score.due_score(None, now=NOW), 0.3)

    def test_past_and_within_24h_are_one(self):
        self.assertEqual(memory_score.due_score(NOW - timedelta(hours=1), now=NOW), 1.0)
        self.assertEqual(memory_score.due_score(NOW + timedelta(hours=12), now=NOW), 1.0)

    def test_linear_decay_after_24h(self):
        self.assertAlmostEqual(
            memory_score.due_score(NOW + timedelta(days=2), now=NOW), 1 - 2 / 7)

    def test_eight_days_is_zero(self):
        self.assertEqual(
            memory_score.due_score(NOW + timedelta(days=8), now=NOW), 0.0)


class ScoreEpisodeTest(unittest.TestCase):
    def test_formula_uses_similarity_decay_and_hits(self):
        item = episode("今天天气很好", hits=3)
        entry = memory_score.score_episode("今天天气", item, now=NOW)
        self.assertEqual(entry["sim"], 1.0)
        self.assertAlmostEqual(entry["rec"], 1.0)
        self.assertAlmostEqual(entry["visit"], 0.3)
        self.assertAlmostEqual(entry["score"], 0.6 + 0.3 + 0.03)

    def test_preference_uses_thirty_day_tau(self):
        old = (NOW - timedelta(days=30)).isoformat(timespec="seconds")
        item = episode("喜欢香菜", kind="preference", created_at=old,
                       updated_at=old)
        entry = memory_score.score_episode("香菜", item, now=NOW)
        self.assertAlmostEqual(entry["rec"], math.exp(-1))

    def test_todo_uses_due_score_not_decay(self):
        old = (NOW - timedelta(days=100)).isoformat(timespec="seconds")
        due = (NOW + timedelta(hours=1)).isoformat(timespec="seconds")
        item = episode("交设计稿", kind="todo", created_at=old, due_at=due)
        entry = memory_score.score_episode("交设计稿", item, now=NOW)
        self.assertEqual(entry["due"], 1.0)
        self.assertEqual(entry["rec"], 1.0)


class RankTest(unittest.TestCase):
    def test_top_k_and_zero_similarity_filter(self):
        items = [
            episode("今天天气很好"),
            episode("今天天气不错"),
            episode("完全无关的内容"),
            episode("今天天气还行"),
        ]
        ranked = memory_score.rank("今天天气", items, k=3, now=NOW)
        self.assertEqual(len(ranked), 3)
        self.assertNotIn("完全无关的内容", [entry["episode"]["text"] for entry in ranked])

    def test_ties_prefer_newer(self):
        older = episode("今天天气", created_at=(NOW - timedelta(days=2)).isoformat(
            timespec="seconds"))
        newer = episode("今天天气", created_at=NOW.isoformat(timespec="seconds"))
        ranked = memory_score.rank("今天天气", [older, newer], k=2, now=NOW)
        self.assertEqual([entry["episode"]["text"] for entry in ranked],
                         ["今天天气", "今天天气"])
        self.assertEqual(ranked[0]["episode"]["created_at"],
                         newer["created_at"])

    def test_due_soon_todo_is_exempt_from_zero_similarity(self):
        todo = episode("交设计稿", kind="todo",
                       due_at=(NOW + timedelta(hours=2)).isoformat(timespec="seconds"))
        ranked = memory_score.rank("天气", [todo], k=3, now=NOW)
        self.assertEqual(len(ranked), 1)

    def test_tombstone_is_skipped(self):
        item = episode("今天天气", status="tombstone")
        self.assertEqual(memory_score.rank("今天天气", [item], now=NOW), [])

    def test_k_zero_is_empty(self):
        self.assertEqual(memory_score.rank("今天天气", [episode("今天天气")], k=0), [])


class TokenCacheTest(unittest.TestCase):
    def test_cache_reuses_and_invalidates_when_text_changes(self):
        cache = memory_score.TokenCache()
        item = episode("今天天气")
        first = cache.tokens(item)
        self.assertIs(first, cache.tokens(item))
        item["text"] = "明天天气"
        second = cache.tokens(item)
        self.assertNotEqual(first, second)
        self.assertEqual(len(cache), 1)
        cache.invalidate(item)
        self.assertEqual(len(cache), 0)

    def test_clear(self):
        cache = memory_score.TokenCache()
        cache.tokens(episode("今天天气"))
        cache.clear()
        self.assertEqual(len(cache), 0)


if __name__ == "__main__":
    unittest.main()
