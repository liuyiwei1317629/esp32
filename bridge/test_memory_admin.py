import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_admin
import memory_store

NOW = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = memory_store.MemoryStore(self.tmp.name, clock=lambda: NOW)
        self.fact = self.store.upsert_fact(
            "name", "用户叫小林", slot="name", now=NOW)
        self.episode = self.store.upsert_episode(
            "todo", "用户明天要交设计稿", slot="todo:设计稿", now=NOW)
        self.deleted = self.store.upsert_fact(
            "fact", "用户养了猫", slot="pet", now=NOW)
        self.store.delete_fact(self.deleted["id"], now=NOW)

    def run_cli(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = memory_admin.main(["--dir", self.tmp.name] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_only_active_and_supports_kind_filter(self):
        code, out, _err = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn(self.fact["id"], out)
        self.assertIn(self.episode["id"], out)
        self.assertNotIn(self.deleted["id"], out)

        code, out, _err = self.run_cli("list", "--kind", "todo")
        self.assertEqual(code, 0)
        self.assertIn(self.episode["id"], out)
        self.assertNotIn(self.fact["id"], out)

    def test_list_json(self):
        code, out, _err = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data["facts"]), 1)
        self.assertEqual(len(data["episodes"]), 1)

    def test_pin_and_unpin(self):
        code, _out, _err = self.run_cli("pin", self.fact["id"])
        self.assertEqual(code, 0)
        self.assertTrue(self.store.find_fact(self.fact["id"])["pinned"])
        code, _out, _err = self.run_cli("unpin", self.fact["id"])
        self.assertEqual(code, 0)
        self.assertFalse(self.store.find_fact(self.fact["id"])["pinned"])

    def test_pin_missing_returns_one(self):
        code, _out, err = self.run_cli("pin", "nope")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_delete_tombstones_then_purge(self):
        code, _out, _err = self.run_cli("delete", self.fact["id"])
        self.assertEqual(code, 0)
        self.assertEqual(self.store.find_fact(self.fact["id"])["status"],
                         "tombstone")
        code, _out, _err = self.run_cli("delete", self.fact["id"], "--purge")
        self.assertEqual(code, 0)
        self.assertIsNone(self.store.find_fact(self.fact["id"]))

    def test_export_contains_only_active_content(self):
        code, out, _err = self.run_cli("export", "--session", "dev1")
        self.assertEqual(code, 0)
        data = json.loads(out)
        ids = {item["id"] for item in data["facts"] + data["episodes"]}
        self.assertIn(self.fact["id"], ids)
        self.assertIn(self.episode["id"], ids)
        self.assertNotIn(self.deleted["id"], ids)
        self.assertIn("session", data)

    def test_export_to_file(self):
        path = os.path.join(self.tmp.name, "export.json")
        code, _out, _err = self.run_cli("export", "--out", path)
        self.assertEqual(code, 0)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("facts", data)
        self.assertNotIn("debug", data)

    def test_delete_todo_clears_session_sticky(self):
        self.store.set_sticky("dev1", "交设计稿",
                              todo_id=self.episode["id"], now=NOW)
        code, _out, _err = self.run_cli("delete", self.episode["id"])
        self.assertEqual(code, 0)
        session = self.store.load_session("dev1")
        self.assertEqual(session["sticky_goal"], "")
        self.assertIsNone(session["sticky_todo_id"])

    def test_stats_reports_pending_promotion(self):
        record = self.store.upsert_episode("preference", "用户喜欢香菜",
                                           slot="pref:香菜", now=NOW)
        for _ in range(3):
            self.store.mark_used(episodes=[record["id"]], now=NOW,
                                 persist=True)
        code, out, _err = self.run_cli("stats")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["pending_promotion"], 1)

    def test_stats_reports_counts_and_capacity(self):
        for index in range(3):
            self.store.upsert_fact("name", "用户叫%d" % index,
                                   slot="name%d" % index, pinned=True, now=NOW)
        self.store.enforce_capacity(facts_max=2, episodes_max=500)
        code, out, _err = self.run_cli("stats")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["at_capacity"])
        self.assertTrue(data["pinned_overflow"])
        self.assertGreaterEqual(data["facts_active"], 3)
        self.assertIn("directory_size", data)

    def test_why_shows_history(self):
        changed = self.store.upsert_fact(
            "name", "用户叫阿杰", slot="name", now=NOW)
        code, out, _err = self.run_cli("why", changed["id"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["record"]["text"], "用户叫阿杰")
        self.assertEqual(data["history"][0]["text"], "用户叫小林")

    def test_why_missing_returns_one(self):
        code, _out, err = self.run_cli("why", "nope")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()
