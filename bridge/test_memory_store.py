import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_store

FIXED = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [line for line in fh.read().splitlines() if line.strip()]


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = memory_store.MemoryStore(self.tmp.name, clock=lambda: FIXED)

    def fact(self, text="用户叫小林", kind="name", slot="name", **kwargs):
        return self.store.upsert_fact(
            kind, text, slot=slot, now=FIXED, **kwargs)


class FactsTest(StoreTestCase):
    def test_roundtrip_creates_one_jsonl_line(self):
        record = self.fact()
        self.assertEqual(record["text"], "用户叫小林")
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["hits"], 0)
        self.assertIsNone(record["last_used_at"])
        lines = read_lines(self.store.facts_path)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], record["id"])

    def test_new_store_reads_existing_records(self):
        record = self.fact()
        other = memory_store.MemoryStore(self.tmp.name)
        self.assertEqual([item["id"] for item in other.active_facts()], [record["id"]])

    def test_same_id_does_not_count_mentions_as_hits(self):
        now2 = FIXED + timedelta(minutes=5)
        first = self.fact()
        second = self.store.upsert_fact(
            "name", "用户叫小林", slot="name", now=now2)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["hits"], 0)
        self.assertIsNone(second["last_used_at"])
        self.assertEqual(second["revision"], 1)
        self.assertEqual(second["created_at"], "2026-10-23T01:00:00+00:00")
        self.assertEqual(len(self.store.active_facts()), 1)

    def test_slot_conflict_keeps_id_and_records_history(self):
        first = self.fact("用户叫小明", slot="name")
        second = self.store.upsert_fact(
            "name", "用户叫小林", slot="name", now=FIXED + timedelta(days=1))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["text"], "用户叫小林")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["history"][0]["text"], "用户叫小明")
        self.assertEqual(second["history"][0]["revision"], 1)
        self.assertEqual(len(self.store.active_facts()), 1)

    def test_tombstone_is_not_revived_by_normal_extraction(self):
        record = self.fact()
        self.assertTrue(self.store.delete_fact(record["id"]))
        again = self.store.upsert_fact("name", "用户叫小林", slot="name")
        self.assertEqual(again["status"], "tombstone")
        self.assertEqual(self.store.active_facts(), [])
        self.assertEqual(self.store.counters().get("tombstone_ignored"), 1)

    def test_tombstone_is_revived_by_explicit_remember(self):
        record = self.fact()
        self.store.delete_fact(record["id"])
        revived = self.store.upsert_fact(
            "name", "用户叫小林", slot="name", explicit=True,
            source="explicit", now=FIXED + timedelta(days=2))
        self.assertEqual(revived["status"], "active")
        self.assertIsNone(revived["tombstone_reason"])
        self.assertEqual(revived["revision"], 3)
        self.assertTrue(revived["pinned"])
        self.assertEqual(revived["source"], "explicit")
        self.assertEqual(len(revived["history"]), 2)

    def test_explicit_remember_pins_an_existing_fact(self):
        record = self.fact(pinned=False)
        pinned = self.store.upsert_fact(
            "name", "用户叫小林", slot="name", explicit=True)
        self.assertTrue(pinned["pinned"])
        self.assertEqual(pinned["source"], "explicit")
        self.assertEqual(pinned["revision"], record["revision"])

    def test_set_pinned_and_delete(self):
        record = self.fact()
        self.assertTrue(self.store.set_pinned(record["id"], True))
        self.assertTrue(self.store.find_fact(record["id"])["pinned"])
        self.assertTrue(self.store.delete_fact(record["id"], reason="deleted"))
        self.assertFalse(self.store.delete_fact(record["id"]))
        tomb = self.store.find_fact(record["id"])
        self.assertEqual(tomb["tombstone_reason"], "deleted")
        self.assertEqual(tomb["status"], "tombstone")

    def test_rejects_empty_and_overlong_text(self):
        with self.assertRaises(ValueError):
            self.store.upsert_fact("fact", "   ")
        with self.assertRaises(ValueError):
            self.store.upsert_fact("fact", "长" * 201)
        with self.assertRaises(ValueError):
            self.store.upsert_fact("todo", "未知 kind 不能进 facts")


class EpisodesTest(StoreTestCase):
    def test_roundtrip_with_utc_due_and_event(self):
        due = FIXED + timedelta(days=7)
        record = self.store.upsert_episode(
            "todo", "用户下周要交设计稿", slot="todo:设计稿",
            due_at=due, now=FIXED)
        self.assertEqual(record["due_at"], due.isoformat(timespec="seconds"))
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["hits"], 0)
        self.assertIsNone(record["last_used_at"])
        self.assertEqual(len(read_lines(self.store.episodes_path)), 1)

    def test_same_slot_updates_text_and_history(self):
        first = self.store.upsert_episode(
            "todo", "用户明天要交设计稿", slot="todo:设计稿", now=FIXED)
        second = self.store.upsert_episode(
            "todo", "用户后天要交设计稿", slot="todo:设计稿",
            now=FIXED + timedelta(hours=1))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["history"][0]["text"], "用户明天要交设计稿")

    def test_mentions_do_not_increment_hits(self):
        first = self.store.upsert_episode(
            "preference", "用户喜欢香菜", slot="pref:香菜", now=FIXED)
        second = self.store.upsert_episode(
            "preference", "用户喜欢香菜", slot="pref:香菜",
            now=FIXED + timedelta(hours=1))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["hits"], 0)
        self.assertIsNone(second["last_used_at"])

    def test_tombstone_does_not_revive(self):
        record = self.store.upsert_episode("todo", "用户要交设计稿",
                                           slot="todo:设计稿", now=FIXED)
        self.store.delete_episode(record["id"])
        again = self.store.upsert_episode("todo", "用户要交设计稿",
                                          slot="todo:设计稿", now=FIXED)
        self.assertEqual(again["status"], "tombstone")
        self.assertEqual(self.store.active_episodes(), [])

    def test_mark_used_batch_updates_hits(self):
        one = self.store.upsert_episode("todo", "用户要交设计稿",
                                        slot="todo:a", now=FIXED)
        two = self.store.upsert_episode("todo", "用户要买牛奶",
                                        slot="todo:b", now=FIXED)
        later = FIXED + timedelta(hours=2)
        self.assertTrue(self.store.mark_used(episodes=[one["id"], two["id"]],
                                             now=later))
        records = {item["id"]: item for item in self.store.active_episodes()}
        self.assertEqual(records[one["id"]]["hits"], 1)
        self.assertEqual(records[two["id"]]["last_used_at"],
                         later.isoformat(timespec="seconds"))


class CapacityTest(StoreTestCase):
    def test_unpinned_facts_are_evicted_first(self):
        pinned = [self.store.upsert_fact("name", "用户叫甲%d" % index,
                                        slot="name%d" % index, pinned=True,
                                        now=FIXED) for index in range(2)]
        unpinned = [self.store.upsert_fact("fact", "用户事实%d" % index,
                                          slot="fact%d" % index, now=FIXED)
                    for index in range(3)]
        self.store.enforce_capacity(facts_max=2, episodes_max=500)
        active = self.store.active_facts()
        self.assertEqual(len(active), 2)
        self.assertEqual({item["id"] for item in active},
                         {item["id"] for item in pinned})
        self.assertFalse(self.store.state["at_capacity"])

    def test_pinned_over_capacity_sets_at_capacity_and_pauses_auto_write(self):
        for index in range(3):
            self.store.upsert_fact("name", "用户叫%d" % index,
                                   slot="name%d" % index, pinned=True, now=FIXED)
        self.store.enforce_capacity(facts_max=2, episodes_max=500)
        self.assertEqual(len(self.store.active_facts()), 3)
        self.assertTrue(self.store.state["at_capacity"])
        self.assertFalse(self.store.can_auto_write())

    def test_episode_capacity_evicts_weakest(self):
        for index in range(3):
            self.store.upsert_episode("event", "事件%d" % index,
                                      slot="event%d" % index, now=FIXED)
        self.store.enforce_capacity(facts_max=200, episodes_max=2)
        self.assertEqual(len(self.store.active_episodes()), 2)


class SessionTest(StoreTestCase):
    def test_default_session_and_roundtrip(self):
        default = self.store.load_session("dev1")
        self.assertEqual(default["summary"], "")
        self.assertEqual(default["recent_turns"], [])
        saved = self.store.save_session("dev1", {
            "summary": "最近有点累",
            "sticky_goal": "交设计稿",
            "recent_turns": [
                {"user": "u" * 100, "assistant": "a" * 100,
                 "ts": "t1", "gen": 1},
                {"user": "u2", "assistant": "a2", "ts": "t2", "gen": 2},
            ],
            "completed_turns": 3,
        }, now=FIXED)
        self.assertEqual(saved["summary"], "最近有点累")
        self.assertEqual(len(saved["recent_turns"]), 2)
        self.assertEqual(len(saved["recent_turns"][1]["user"]), 2)
        self.assertEqual(len(saved["recent_turns"][0]["user"]), 40)
        self.assertEqual(len(saved["recent_turns"][0]["assistant"]), 50)
        loaded = memory_store.MemoryStore(self.tmp.name).load_session("dev1")
        self.assertEqual(loaded["sticky_goal"], "交设计稿")
        self.assertEqual(loaded["completed_turns"], 3)

    def test_recent_turns_drop_gen_from_disk_and_memory(self):
        self.store.save_session("dev1", {
            "recent_turns": [
                {"user": "u", "assistant": "a", "ts": "t1", "gen": 7},
            ],
        }, now=FIXED)
        loaded = self.store.load_session("dev1")
        self.assertNotIn("gen", loaded["recent_turns"][0])
        with open(self.store.session_path("dev1"), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertNotIn("gen", raw["recent_turns"][0])

    def test_different_session_ids_have_different_files(self):
        self.store.save_session("dev1", {"summary": "one"}, now=FIXED)
        self.store.save_session("dev2", {"summary": "two"}, now=FIXED)
        self.assertNotEqual(self.store.session_path("dev1"),
                            self.store.session_path("dev2"))
        self.assertEqual(self.store.load_session("dev1")["summary"], "one")

    def test_sticky_clear_only_for_matching_todo(self):
        self.store.set_sticky("dev1", "交设计稿", todo_id="e1", now=FIXED)
        self.assertFalse(self.store.clear_sticky_if("dev1", "e2"))
        self.assertTrue(self.store.clear_sticky_if("dev1", "e1"))
        self.assertEqual(self.store.load_session("dev1")["sticky_goal"], "")

    def test_session_id_with_path_characters_stays_in_sessions_dir(self):
        sid = "../../evil"
        path = self.store.session_path(sid)
        self.assertEqual(os.path.dirname(path), self.store.sessions_dir)
        self.store.save_session(sid, {"summary": "safe"}, now=FIXED)
        self.assertTrue(os.path.exists(path))

    def test_invalid_session_field_falls_back_to_bak(self):
        self.store.save_session(
            "dev1", {"summary": "旧版", "completed_turns": 2}, now=FIXED)
        self.store.save_session(
            "dev1", {"summary": "新版", "completed_turns": 3},
            now=FIXED + timedelta(minutes=1))
        path = self.store.session_path("dev1")
        self.assertTrue(os.path.exists(path + ".bak"))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"session_id": "dev1", "summary": "坏字段",
                       "completed_turns": "oops"}, fh)
        loaded = memory_store.MemoryStore(self.tmp.name).load_session("dev1")
        self.assertEqual(loaded["summary"], "旧版")
        self.assertEqual(loaded["completed_turns"], 2)


class PersistenceTest(StoreTestCase):
    def test_bak_restores_when_main_file_is_corrupt(self):
        first = self.fact("用户叫小明")
        self.fact("用户叫小林")
        self.assertTrue(os.path.exists(self.store.facts_path + ".bak"))
        with open(self.store.facts_path, "w", encoding="utf-8") as fh:
            fh.write("{not json}\n")
        other = memory_store.MemoryStore(self.tmp.name)
        records = other.facts()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], first["id"])
        corrupt = [name for name in os.listdir(self.store.root)
                   if name.startswith("facts.jsonl.corrupt.")]
        self.assertEqual(len(corrupt), 1)

    def test_bad_lines_are_skipped_and_counted(self):
        record = self.fact()
        with open(self.store.facts_path, "a", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"kind": "alien", "text": "x"}) + "\n")
        other = memory_store.MemoryStore(self.tmp.name)
        self.assertEqual([item["id"] for item in other.facts()], [record["id"]])
        counts = other.bad_line_counts()
        self.assertEqual(counts["facts"], 1)
        self.assertEqual(counts["unknown_kinds"], 1)

    def test_mtime_change_is_reloaded(self):
        first = memory_store.MemoryStore(self.tmp.name)
        second = memory_store.MemoryStore(self.tmp.name)
        self.assertEqual(second.active_facts(), [])
        first.upsert_fact("name", "用户叫小林", slot="name", now=FIXED)
        self.assertEqual(len(second.active_facts()), 1)

    def test_higher_schema_is_read_only(self):
        self.store.save_state(self.store.default_state())
        with open(self.store.state_path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 99, "counters": {}}, fh)
        other = memory_store.MemoryStore(self.tmp.name)
        self.assertTrue(other.read_only)
        with self.assertRaises(memory_store.MemoryReadOnly):
            other.upsert_fact("name", "用户叫小林", slot="name")

    def test_lock_busy_raises_instead_of_corrupting(self):
        self.store._ensure_root()
        with open(self.store.lock_path, "w", encoding="utf-8") as fh:
            fh.write("999 0")
        store = memory_store.MemoryStore(self.tmp.name, lock_timeout=0.05,
                                         stale_lock_seconds=60)
        with self.assertRaises(memory_store.MemoryBusy):
            store.upsert_fact("name", "用户叫小林", slot="name")
        os.unlink(self.store.lock_path)

    def test_no_tmp_files_left_behind(self):
        self.fact()
        self.store.save_session("dev1", {"summary": "s"})
        leftovers = [name for name in os.listdir(self.store.root)
                     if ".tmp." in name]
        self.assertEqual(leftovers, [])

    def test_failed_record_write_invalidates_cache(self):
        from unittest import mock

        with mock.patch.object(self.store, "_atomic_write",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.fact()
            with self.assertRaises(OSError):
                self.store.upsert_episode(
                    "todo", "用户要交设计稿", slot="todo:x", now=FIXED)
        self.assertEqual(self.store.active_facts(), [])
        self.assertEqual(self.store.active_episodes(), [])
        other = memory_store.MemoryStore(self.tmp.name)
        self.assertEqual(other.active_facts(), [])
        self.assertEqual(other.active_episodes(), [])

    def test_failed_tombstone_write_keeps_disk_state(self):
        from unittest import mock

        record = self.fact()
        with mock.patch.object(self.store, "_atomic_write",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.delete_fact(record["id"])
        self.assertEqual(self.store.find_fact(record["id"])["status"], "active")
        self.assertEqual([item["id"] for item in self.store.active_facts()],
                         [record["id"]])

    def test_failed_state_write_does_not_change_visible_state(self):
        from unittest import mock

        with mock.patch.object(self.store, "_atomic_write_json",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.set_at_capacity(True)
        self.assertFalse(self.store.state["at_capacity"])
        other = memory_store.MemoryStore(self.tmp.name)
        self.assertFalse(other.state["at_capacity"])

    def test_failed_counter_write_is_not_kept_in_memory(self):
        from unittest import mock

        with mock.patch.object(self.store, "_atomic_write_json",
                               side_effect=OSError("disk full")):
            self.store.bump_counter("lost")
        self.assertNotIn("lost", self.store.counters())

    def test_two_instances_keep_both_counters(self):
        first = memory_store.MemoryStore(self.tmp.name, clock=lambda: FIXED)
        second = memory_store.MemoryStore(self.tmp.name, clock=lambda: FIXED)
        first.bump_counter("a")
        second.bump_counter("b")
        other = memory_store.MemoryStore(self.tmp.name)
        self.assertEqual(other.counters(), {"a": 1, "b": 1})

    def test_capacity_update_keeps_other_instance_counter(self):
        first = memory_store.MemoryStore(self.tmp.name, clock=lambda: FIXED)
        second = memory_store.MemoryStore(self.tmp.name, clock=lambda: FIXED)
        first.bump_counter("a")
        second.set_at_capacity(True)
        other = memory_store.MemoryStore(self.tmp.name)
        self.assertTrue(other.state["at_capacity"])
        self.assertEqual(other.counters(), {"a": 1})

    def test_bump_counter_keeps_other_instance_capacity(self):
        first = memory_store.MemoryStore(self.tmp.name, clock=lambda: FIXED)
        second = memory_store.MemoryStore(self.tmp.name, clock=lambda: FIXED)
        first.set_at_capacity(True)
        second.bump_counter("b")
        other = memory_store.MemoryStore(self.tmp.name)
        self.assertTrue(other.state["at_capacity"])
        self.assertEqual(other.counters(), {"b": 1})

    def test_replace_permission_error_is_retried(self):
        from unittest import mock

        real_replace = os.replace
        attempts = []

        def flaky_replace(src, dst):
            attempts.append(dst)
            if len(attempts) < 3:
                raise PermissionError(13, "Access denied", dst)
            return real_replace(src, dst)

        with mock.patch.object(memory_store.os, "replace",
                               side_effect=flaky_replace):
            record = self.fact()
        self.assertEqual(len(attempts), 3)
        self.assertEqual(record["text"], "用户叫小林")
        self.assertTrue(os.path.exists(self.store.facts_path))
        leftovers = [name for name in os.listdir(self.store.root)
                     if ".tmp." in name]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
