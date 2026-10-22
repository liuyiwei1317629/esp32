import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usage

FIXED = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def stats(profile="deepseek", fallback=True, usage_obj=None):
    return {
        "profile": profile,
        "fallback_used": fallback,
        "attempts": 2,
        "usage": usage_obj if usage_obj is not None else {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
        },
        "content_chars": 99,
        "user_text": "这句话不能进账本",
    }


class EstimateCostTest(unittest.TestCase):
    def test_prices_are_per_million_tokens(self):
        cost = usage.estimate_cost({"prompt_tokens": 1_000_000,
                                    "completion_tokens": 500_000}, 2.0, 8.0)
        self.assertEqual(cost, 6.0)

    def test_missing_tokens_count_as_zero(self):
        self.assertEqual(usage.estimate_cost({}, 2.0, 8.0), 0.0)

    def test_no_usage_or_unpriced_profile_returns_none(self):
        self.assertIsNone(usage.estimate_cost(None, 2.0, 8.0))
        self.assertIsNone(usage.estimate_cost({"prompt_tokens": 1}, None, 8.0))
        self.assertIsNone(usage.estimate_cost({"prompt_tokens": 1}, 2.0, None))

    def test_local_profile_can_be_free(self):
        self.assertEqual(usage.estimate_cost({"prompt_tokens": 1000,
                                              "completion_tokens": 50}, 0.0, 0.0), 0.0)


class EntryTest(unittest.TestCase):
    def test_entry_contains_only_ledger_fields(self):
        entry = usage.build_entry(stats(), 2.0, 8.0, now=FIXED)
        self.assertEqual(set(entry), {
            "ts", "profile", "prompt_tokens", "completion_tokens",
            "cost_cny", "fallback_used",
        })
        self.assertEqual(entry["ts"], "2026-10-22T12:00:00+00:00")
        self.assertEqual(entry["profile"], "deepseek")
        self.assertEqual(entry["prompt_tokens"], 1000)
        self.assertEqual(entry["completion_tokens"], 50)
        self.assertEqual(entry["cost_cny"], 0.0024)
        self.assertTrue(entry["fallback_used"])

    def test_user_text_and_other_stats_are_not_recorded(self):
        entry = usage.build_entry(stats(), 2.0, 8.0, now=FIXED)
        raw = json.dumps(entry, ensure_ascii=False)
        self.assertNotIn("这句话不能进账本", raw)
        self.assertNotIn("content_chars", entry)
        self.assertNotIn("attempts", entry)

    def test_missing_profile_or_usage_skips(self):
        self.assertIsNone(usage.build_entry({"usage": {"prompt_tokens": 1}},
                                            2.0, 8.0, now=FIXED))
        no_usage = stats()
        no_usage["usage"] = None
        self.assertIsNone(usage.build_entry(no_usage, 2.0, 8.0, now=FIXED))


class AppendTest(unittest.TestCase):
    def test_appends_one_json_line_and_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "usage.jsonl")
            first = usage.build_entry(stats(), 2.0, 8.0, now=FIXED)
            second = usage.build_entry(stats(profile="kimi", fallback=False),
                                       1.0, 4.0, now=FIXED)
            usage.append_entry(first, path)
            usage.append_entry(second, path)
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0]), first)
        self.assertEqual(json.loads(lines[1])["profile"], "kimi")
        self.assertTrue(lines[0].endswith("\n"))

    def test_record_turn_uses_price_lookup_and_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            seen = []

            def lookup(name):
                seen.append(name)
                return 2.0, 8.0

            entry = usage.record_turn(stats(), price_lookup=lookup, path=path, now=FIXED)
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        self.assertEqual(seen, ["deepseek"])
        self.assertEqual(json.loads(raw), entry)

    def test_record_turn_skips_turns_without_profile_or_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            no_profile = stats(profile=None)
            self.assertIsNone(usage.record_turn(no_profile, path=path, now=FIXED))
            no_usage = stats()
            no_usage["usage"] = None
            self.assertIsNone(usage.record_turn(no_usage, path=path, now=FIXED))
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
