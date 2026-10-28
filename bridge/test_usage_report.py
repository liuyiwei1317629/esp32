import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import usage_report

CN = timezone(timedelta(hours=8))


def entry(ts, cost=0.001, profile="deepseek"):
    return {
        "ts": ts,
        "profile": profile,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost_cny": cost,
        "fallback_used": False,
    }


class SummarizeTest(unittest.TestCase):
    def test_today_uses_local_date_not_utc(self):
        # 本地 2026-10-28 01:00（UTC 2026-10-27 17:00）
        now = datetime(2026, 10, 28, 1, 0, tzinfo=CN)
        rows = [
            entry("2026-10-27T16:30:00+00:00", 0.002),   # 本地 10-28 00:30 → 今天
            entry("2026-10-27T15:30:00+00:00", 0.003),   # 本地 10-27 23:30 → 昨天
        ]
        summary = usage_report.summarize(rows, now=now, tz=CN)
        self.assertEqual(summary["day"], "2026-10-28")
        self.assertEqual(summary["today_cny"], 0.002)
        self.assertEqual(summary["month_cny"], 0.005)
        self.assertEqual(summary["total_cny"], 0.005)
        self.assertEqual(summary["turns"], 2)

    def test_month_boundary_uses_local_date(self):
        # 本地 2026-11-01 00:30（UTC 2026-10-31 16:30）
        now = datetime(2026, 11, 1, 0, 30, tzinfo=CN)
        rows = [
            entry("2026-10-31T15:50:00+00:00", 0.010),   # 本地 10-31 23:50 → 上月
            entry("2026-10-31T16:10:00+00:00", 0.020),   # 本地 11-01 00:10 → 本月
        ]
        summary = usage_report.summarize(rows, now=now, tz=CN)
        self.assertEqual(summary["month"], "2026-11")
        self.assertEqual(summary["today_cny"], 0.020)
        self.assertEqual(summary["month_cny"], 0.020)
        self.assertEqual(summary["total_cny"], 0.030)

    def test_unpriced_turns_are_counted_but_not_priced(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=CN)
        rows = [
            entry("2026-10-28T03:00:00+00:00", 0.004),
            entry("2026-10-28T03:10:00+00:00", None),
            entry("2026-10-28T03:20:00+00:00", None),
        ]
        summary = usage_report.summarize(rows, now=now, tz=CN)
        self.assertEqual(summary["turns"], 3)
        self.assertEqual(summary["unpriced_turns"], 2)
        self.assertEqual(summary["today_cny"], 0.004)
        self.assertEqual(summary["by_profile"]["deepseek"]["turns"], 3)

    def test_profiles_are_split(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=CN)
        rows = [
            entry("2026-10-28T03:00:00+00:00", 0.004, "deepseek"),
            entry("2026-10-28T03:05:00+00:00", 0.0, "local"),
        ]
        summary = usage_report.summarize(rows, now=now, tz=CN)
        self.assertEqual(summary["today_cny"], 0.004)
        self.assertEqual(summary["by_profile"]["deepseek"]["today_cny"], 0.004)
        self.assertEqual(summary["by_profile"]["local"]["today_cny"], 0.0)
        self.assertEqual(summary["by_profile"]["local"]["turns"], 1)

    def test_malformed_entries_are_skipped(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=CN)
        rows = [
            entry("2026-10-28T03:00:00+00:00", 0.004),
            {"profile": "deepseek", "cost_cny": 0.5},          # 缺 ts
            {"ts": "not-a-time", "cost_cny": 0.5},             # ts 坏
            {"ts": "2026-10-28T03:30:00+00:00"},               # 缺 profile
        ]
        summary = usage_report.summarize(rows, now=now, tz=CN)
        self.assertEqual(summary["turns"], 1)
        self.assertEqual(summary["total_cny"], 0.004)

    def test_empty_ledger(self):
        summary = usage_report.summarize([], now=datetime(2026, 10, 28, 12, 0, tzinfo=CN),
                                        tz=CN)
        self.assertEqual(summary["turns"], 0)
        self.assertEqual(summary["today_cny"], 0.0)
        self.assertEqual(summary["month_cny"], 0.0)
        self.assertEqual(summary["total_cny"], 0.0)
        self.assertEqual(summary["by_profile"], {})

    def test_amounts_are_rounded(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=CN)
        rows = [entry("2026-10-28T03:00:00+00:00", 0.1),
                entry("2026-10-28T03:01:00+00:00", 0.2)]
        summary = usage_report.summarize(rows, now=now, tz=CN)
        self.assertEqual(summary["today_cny"], 0.3)

    def test_naive_timestamp_is_treated_as_utc(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=CN)
        summary = usage_report.summarize([entry("2026-10-28T03:00:00", 0.002)],
                                        now=now, tz=CN)
        self.assertEqual(summary["today_cny"], 0.002)


class LoadTest(unittest.TestCase):
    def test_load_reads_jsonl_and_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(entry("2026-10-28T03:00:00+00:00")) + "\n")
                fh.write("{not json\n")
                fh.write("\n")
                fh.write(json.dumps(entry("2026-10-28T03:05:00+00:00")) + "\n")
            rows = usage_report.load(path)
        self.assertEqual(len(rows), 2)

    def test_load_missing_file_is_empty(self):
        self.assertEqual(usage_report.load("Z:/does-not-exist/usage.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
