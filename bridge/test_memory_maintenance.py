import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_maintenance
import memory_store

NOW = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = memory_store.MemoryStore(self.tmp.name, clock=lambda: NOW)
        self.maintenance = memory_maintenance.MemoryMaintenance(
            self.store, clock=lambda: NOW)

    def age(self, days):
        return NOW - timedelta(days=days)

    def preference_with_recalls(self, text="用户喜欢香菜", recalls=3):
        record = self.store.upsert_episode(
            "preference", text, slot="pref:香菜", now=NOW)
        for _ in range(recalls):
            self.store.mark_used(episodes=[record["id"]], now=NOW,
                                 persist=False)
        return self.store.find_episode(record["id"])

    def test_preference_recalled_three_times_promotes_and_tombstones_episode(self):
        episode = self.preference_with_recalls()
        self.assertEqual(episode["hits"], 3)
        result = self.maintenance.run(now=NOW)
        self.assertEqual(result["promoted"], 1)
        facts = self.store.active_facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["kind"], "preference")
        self.assertEqual(facts[0]["source"], "promoted")
        self.assertFalse(facts[0]["pinned"])
        tomb = self.store.find_episode(episode["id"])
        self.assertEqual(tomb["status"], "tombstone")
        self.assertEqual(tomb["tombstone_reason"], "promoted")

    def test_three_mentions_do_not_promote(self):
        record = None
        for _ in range(3):
            record = self.store.upsert_episode(
                "preference", "用户喜欢香菜", slot="pref:香菜", now=NOW)
        self.assertEqual(record["hits"], 0)
        result = self.maintenance.run(now=NOW)
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(self.store.active_facts(), [])
        self.assertEqual(len(self.store.active_episodes()), 1)

    def test_demoted_preference_recalled_three_times_is_not_lost(self):
        fact = self.store.upsert_fact(
            "preference", "用户喜欢香菜", slot="pref:香菜",
            now=self.age(40))
        self.maintenance.run(now=NOW)
        episodes = self.store.active_episodes()
        self.assertEqual(len(episodes), 1)
        episode = episodes[0]
        self.assertEqual(self.store.find_fact(fact["id"])["status"],
                         "tombstone")
        for _ in range(3):
            self.store.mark_used(episodes=[episode["id"]], now=NOW,
                                 persist=False)
        result = self.maintenance.run(now=NOW)
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(self.store.active_facts(), [])
        active = self.store.active_episodes()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], episode["id"])
        self.assertEqual(active[0]["status"], "active")
        self.assertFalse(
            self.store.find_fact(fact["id"])["status"] == "tombstone"
            and active[0]["status"] == "tombstone")

    def test_only_preference_promotes(self):
        for kind, slot in (("name", "name"), ("relation", "relation:妈妈"),
                           ("residence", "city"), ("todo", "todo:x")):
            record = self.store.upsert_episode(
                kind, "用户内容" + kind, slot=slot, now=NOW)
            for _ in range(3):
                self.store.mark_used(episodes=[record["id"]], now=NOW,
                                     persist=False)
        result = self.maintenance.run(now=NOW)
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(self.store.active_facts(), [])
        self.assertEqual(len(self.store.active_episodes()), 4)

    def test_thirty_days_demotes_unpinned_fact(self):
        fact = self.store.upsert_fact(
            "preference", "用户喜欢香菜", slot="pref:香菜",
            now=self.age(40))
        result = self.maintenance.run(now=NOW)
        self.assertEqual(result["demoted"], 1)
        tomb = self.store.find_fact(fact["id"])
        self.assertEqual(tomb["status"], "tombstone")
        self.assertEqual(tomb["tombstone_reason"], "demoted")
        episodes = self.store.active_episodes()
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["source"], "demoted")
        self.assertEqual(episodes[0]["fact_id"], fact["id"])

    def test_promoted_preference_can_be_demoted_back_to_episode(self):
        episode = self.preference_with_recalls()
        self.maintenance.run(now=NOW)
        fact = self.store.active_facts()[0]
        later = NOW + timedelta(days=40)
        result = self.maintenance.run(now=later)
        self.assertEqual(result["demoted"], 1)
        self.assertEqual(self.store.find_fact(fact["id"])["status"],
                         "tombstone")
        active = self.store.active_episodes()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "active")
        self.assertEqual(active[0]["source"], "demoted")
        self.assertEqual(active[0]["fact_id"], fact["id"])
        self.assertEqual(active[0]["text"], episode["text"])
        again = self.maintenance.run(now=later)
        self.assertEqual(again["promoted"], 0)
        self.assertEqual(again["demoted"], 0)
        self.assertEqual(len(self.store.active_episodes()), 1)

    def test_eighteen_hundred_days_expires_without_episode(self):
        fact = self.store.upsert_fact(
            "preference", "用户喜欢香菜", slot="pref:香菜",
            now=self.age(200))
        result = self.maintenance.run(now=NOW)
        self.assertEqual(result["expired_facts"], 1)
        self.assertEqual(self.store.active_episodes(), [])
        tomb = self.store.find_fact(fact["id"])
        self.assertEqual(tomb["tombstone_reason"], "expired")

    def test_pinned_fact_is_never_demoted(self):
        fact = self.store.upsert_fact(
            "name", "用户叫小林", slot="name", pinned=True,
            now=self.age(400))
        result = self.maintenance.run(now=NOW)
        self.assertEqual(result["demoted"], 0)
        self.assertEqual(result["expired_facts"], 0)
        self.assertEqual(self.store.find_fact(fact["id"])["status"], "active")

    def test_episode_180_days_without_hit_expires(self):
        episode = self.store.upsert_episode(
            "event", "用户去年聊过旅行", slot="event:旅行",
            now=self.age(200))
        result = self.maintenance.run(now=NOW)
        self.assertEqual(result["expired_episodes"], 1)
        self.assertEqual(self.store.find_episode(episode["id"])["status"],
                         "tombstone")

    def test_tombstone_older_than_one_year_is_purged(self):
        fact = self.store.upsert_fact("name", "用户叫小林", slot="name",
                                      now=self.age(500))
        self.store.delete_fact(fact["id"], now=self.age(400))
        self.assertIsNotNone(self.store.find_fact(fact["id"]))
        result = self.maintenance.run(now=NOW)
        self.assertGreaterEqual(result["purged"], 1)
        self.assertIsNone(self.store.find_fact(fact["id"]))

    def test_run_is_idempotent(self):
        self.preference_with_recalls()
        first = self.maintenance.run(now=NOW)
        second = self.maintenance.run(now=NOW)
        self.assertEqual(first["promoted"], 1)
        self.assertEqual(second["promoted"], 0)
        self.assertEqual(len(self.store.active_facts()), 1)
        self.assertEqual(len(self.store.active_episodes()), 0)
        self.assertEqual(self.store.state["last_maintenance"],
                         NOW.isoformat(timespec="seconds"))

    def test_maybe_run_waits_for_24h_or_50_turns(self):
        self.maintenance.run(now=NOW)
        self.assertIsNone(self.maintenance.maybe_run(
            now=NOW + timedelta(hours=1), completed_turns=10))
        self.assertEqual(self.store.state["turns_since_maintenance"], 10)
        self.assertIsNotNone(self.maintenance.maybe_run(
            now=NOW + timedelta(hours=25), completed_turns=10))
        self.assertEqual(self.store.state["turns_since_maintenance"], 0)
        self.assertIsNotNone(self.maintenance.maybe_run(
            now=NOW + timedelta(hours=1), completed_turns=50))

    def test_read_only_store_does_not_write(self):
        self.store.save_state(self.store.default_state())
        import json
        with open(self.store.state_path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 99, "counters": {}}, fh)
        other = memory_store.MemoryStore(self.tmp.name)
        result = memory_maintenance.MemoryMaintenance(other).run(now=NOW)
        self.assertTrue(result.get("read_only"))


if __name__ == "__main__":
    unittest.main()
