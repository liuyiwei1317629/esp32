import dataclasses
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_context
import memory_maintenance
import memory_store
import memory_writer

NOW = datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc)


class FakeMaintenance:
    def __init__(self, trigger_at=None, fail=False):
        self.calls = []
        self.trigger_at = trigger_at
        self.fail = fail

    def maybe_run(self, now=None, completed_turns=None):
        self.calls.append(completed_turns)
        if self.fail:
            raise RuntimeError("maintenance boom")
        if self.trigger_at is not None and completed_turns == self.trigger_at:
            return {"promoted": 0}
        return None


def make_job(**overrides):
    values = {
        "session_id": "dev1",
        "user": "我叫小林",
        "assistant": "知道啦。",
        "gen": 1,
        "ts": "2026-10-23T02:00:00+00:00",
        "completed": True,
        "error": None,
        "aborted": False,
        "done": True,
        "finish_reason": "stop",
        "selected_ids": (),
    }
    values.update(overrides)
    return memory_writer.WriteJob(**values)


def snapshot(root):
    result = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                result[os.path.relpath(path, root)] = fh.read()
    return result


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.store = memory_store.MemoryStore(self.root, clock=lambda: NOW)

    def make_writer(self, **kwargs):
        kwargs.setdefault("dry_run", False)
        kwargs.setdefault("clock", lambda: NOW)
        writer = memory_writer.MemoryWriter(self.store, **kwargs)
        self.addCleanup(writer.stop)
        return writer

    def test_completed_job_writes_fact_and_session_summary(self):
        writer = self.make_writer()
        self.assertTrue(writer.enqueue(make_job()))
        self.assertTrue(writer.wait_idle())

        facts = self.store.active_facts()
        self.assertEqual([item["text"] for item in facts], ["用户叫小林"])
        session = self.store.load_session("dev1")
        self.assertIn("我叫小林", session["summary"])
        self.assertIn("知道啦", session["summary"])
        self.assertEqual(session["completed_turns"], 1)
        self.assertEqual(session["recent_turns"][-1]["user"], "我叫小林")
        self.assertEqual(writer.counters()["written_jobs"], 1)

    def test_preference_and_todo_route_to_episodes(self):
        writer = self.make_writer()
        self.assertTrue(writer.enqueue(make_job(
            user="我喜欢喝咖啡。我明天要交设计稿", assistant="好呀。")))
        self.assertTrue(writer.wait_idle())

        episodes = self.store.active_episodes()
        texts = [item["text"] for item in episodes]
        self.assertIn("用户喜欢喝咖啡", texts)
        self.assertIn("用户明天要交设计稿", texts)
        todo = [item for item in episodes if item["kind"] == "todo"][0]
        self.assertIsNotNone(todo["due_at"])
        session = self.store.load_session("dev1")
        self.assertEqual(session["sticky_goal"], "用户明天要交设计稿")
        self.assertEqual(session["sticky_todo_id"], todo["id"])

    def test_incomplete_job_is_rejected_without_writing(self):
        writer = self.make_writer()
        before = snapshot(self.root)
        self.assertFalse(writer.enqueue(make_job(completed=False)))
        self.assertTrue(writer.wait_idle())
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(writer.counters()["rejected_ineligible"], 1)
        self.assertEqual(self.store.load_session("dev1")["completed_turns"], 0)

    def test_completed_job_survives_later_abort_and_new_generation(self):
        writer = self.make_writer()
        self.assertTrue(writer.enqueue(make_job(
            completed=True, aborted=True, gen=7)))
        self.assertTrue(writer.wait_idle())
        self.assertEqual([item["text"] for item in self.store.active_facts()],
                         ["用户叫小林"])
        self.assertEqual(writer.counters()["written_jobs"], 1)

    def test_clean_eof_without_done_or_stop_is_rejected(self):
        writer = self.make_writer()
        self.assertFalse(writer.enqueue(make_job(done=False, finish_reason=None)))
        self.assertTrue(writer.enqueue(make_job(
            gen=2, done=False, finish_reason="stop")))
        self.assertTrue(writer.wait_idle())
        self.assertEqual(writer.counters()["rejected_ineligible"], 1)
        self.assertEqual(writer.counters()["written_jobs"], 1)

    def test_length_finish_is_rejected_even_when_done(self):
        writer = self.make_writer()
        self.assertFalse(writer.enqueue(make_job(
            done=True, finish_reason="length")))
        self.assertEqual(writer.counters()["rejected_ineligible"], 1)
        self.assertEqual(self.store.load_session("dev1")["completed_turns"], 0)

    def test_done_without_finish_reason_still_writes(self):
        writer = self.make_writer()
        self.assertTrue(writer.enqueue(make_job(
            done=True, finish_reason=None)))
        self.assertTrue(writer.wait_idle())
        self.assertEqual(writer.counters()["written_jobs"], 1)

    def test_empty_asr_or_reply_is_rejected(self):
        writer = self.make_writer()
        self.assertFalse(writer.enqueue(make_job(user="", gen=1)))
        self.assertFalse(writer.enqueue(make_job(assistant="", gen=2)))
        self.assertEqual(writer.counters()["rejected_ineligible"], 2)
        self.assertFalse(os.path.exists(self.store.facts_path))

    def test_sensitive_turn_is_not_cached_or_enqueued(self):
        writer = self.make_writer()
        self.assertFalse(writer.enqueue(make_job(
            user="我的手机号是13800138000")))
        self.assertEqual(writer.recent_cache.get("dev1"), [])
        self.assertFalse(os.path.exists(self.store.facts_path))
        self.assertEqual(writer.counters()["rejected_sensitive"], 1)

    def test_recent_cache_is_visible_to_next_read_before_writer_runs(self):
        writer = self.make_writer(dry_run=True)
        self.assertTrue(writer.enqueue(make_job(
            user="在忙吗", assistant="在的。")))
        cache = writer.recent_cache.get("dev1")
        self.assertEqual([item["user"] for item in cache], ["在忙吗"])

        messages, info = memory_context.build_for_turn(
            self.store, "人格", "dev1", "现在呢", recent_cache=cache)
        self.assertFalse(info["fallback"])
        self.assertIn("在忙吗", [item["content"] for item in messages])
        self.assertIn("在的。", [item["content"] for item in messages])

    def test_queue_full_drops_new_job_without_raising(self):
        class BlockingWriter(memory_writer.MemoryWriter):
            def __init__(self, *args, **kwargs):
                self.started = threading.Event()
                self.release = threading.Event()
                super().__init__(*args, **kwargs)

            def _process(self, job):
                self.started.set()
                self.release.wait(2)

        writer = BlockingWriter(self.store, dry_run=True, queue_maxsize=1)
        self.addCleanup(writer.stop)
        self.assertTrue(writer.enqueue(make_job(gen=1)))
        self.assertTrue(writer.started.wait(2))
        self.assertTrue(writer.enqueue(make_job(gen=2)))
        self.assertFalse(writer.enqueue(make_job(gen=3)))
        self.assertEqual(writer.counters()["rejected_queue_full"], 1)
        writer.release.set()
        self.assertTrue(writer.wait_idle())

    def test_worker_exception_does_not_stop_the_queue(self):
        class FlakyWriter(memory_writer.MemoryWriter):
            def __init__(self, *args, **kwargs):
                self.calls = 0
                super().__init__(*args, **kwargs)

            def _process(self, job):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")
                return super()._process(job)

        writer = FlakyWriter(self.store, dry_run=False)
        self.addCleanup(writer.stop)
        self.assertTrue(writer.enqueue(make_job(gen=1)))
        self.assertTrue(writer.enqueue(make_job(gen=2)))
        self.assertTrue(writer.wait_idle())
        self.assertEqual(writer.counters()["write_errors"], 1)
        self.assertEqual(writer.counters()["written_jobs"], 1)
        self.assertEqual([item["text"] for item in self.store.active_facts()],
                         ["用户叫小林"])

    def test_dry_run_counts_but_writes_nothing(self):
        writer = self.make_writer(dry_run=True)
        before = snapshot(self.root)
        self.assertTrue(writer.enqueue(make_job()))
        self.assertTrue(writer.wait_idle())

        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(writer.counters()["dry_run_jobs"], 1)
        self.assertEqual(writer.counters()["dry_run_candidates"], 1)
        self.assertEqual(self.store.active_facts(), [])
        self.assertEqual(writer.recent_cache.get("dev1")[0]["user"], "我叫小林")

    def test_hits_are_batched_every_five_completed_turns(self):
        fact = self.store.upsert_fact("name", "用户叫小林", slot="name", now=NOW)
        writer = self.make_writer()
        for gen in range(1, 6):
            self.assertTrue(writer.enqueue(make_job(
                gen=gen, user="你好", assistant="在的。",
                selected_ids=(fact["id"],))))
        self.assertTrue(writer.wait_idle())

        self.assertEqual(self.store.find_fact(fact["id"])["hits"], 5)
        self.assertEqual(writer.counters()["hits_flushes"], 1)

    def test_duplicate_generation_is_processed_once(self):
        writer = self.make_writer()
        self.assertTrue(writer.enqueue(make_job(gen=1)))
        self.assertFalse(writer.enqueue(make_job(gen=1, user="第二问")))
        self.assertTrue(writer.wait_idle())
        self.assertEqual(writer.counters()["rejected_duplicate"], 1)
        self.assertEqual(self.store.load_session("dev1")["completed_turns"], 1)

    def test_recent_cache_keeps_two_turns_per_session(self):
        cache = memory_writer.RecentCache()
        for index in range(3):
            cache.record("dev1", "u%d" % index, "a%d" % index,
                         "2026-10-23T02:0%d:00+00:00" % index)
        self.assertEqual([item["user"] for item in cache.get("dev1")],
                         ["u1", "u2"])
        self.assertEqual(cache.get("other"), [])

    def test_recent_cache_bounds_session_count(self):
        cache = memory_writer.RecentCache(max_sessions=2)
        for sid in ("s1", "s2", "s3"):
            cache.record(sid, "u", "a", "2026-10-23T02:00:00+00:00")
        self.assertEqual(set(cache._items), {"s2", "s3"})
        self.assertEqual(cache.get("s1"), [])
        cache.get("s2")
        cache.record("s4", "u", "a", "2026-10-23T02:00:00+00:00")
        self.assertEqual(set(cache._items), {"s2", "s4"})

    def test_seen_generations_are_bounded(self):
        writer = self.make_writer(seen_gen_limit=2)
        for gen in (1, 2, 3):
            self.assertTrue(writer.enqueue(make_job(
                gen=gen, user="问题%d" % gen)))
        self.assertTrue(writer.wait_idle())
        self.assertEqual(writer._seen_gens, {2, 3})
        self.assertEqual(writer.counters()["written_jobs"], 3)

    def test_recent_turns_are_clipped_in_session(self):
        writer = self.make_writer()
        for index in range(1, 4):
            self.assertTrue(writer.enqueue(make_job(
                gen=index, user="问题%d" % index, assistant="回答%d。" % index,
                ts="2026-10-23T02:0%d:00+00:00" % index)))
        self.assertTrue(writer.wait_idle())
        session = self.store.load_session("dev1")
        self.assertEqual([item["user"] for item in session["recent_turns"]],
                         ["问题2", "问题3"])


class MaintenanceWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = memory_store.MemoryStore(self.tmp.name, clock=lambda: NOW)

    def make_writer(self, maintenance, **kwargs):
        writer = memory_writer.MemoryWriter(
            self.store, dry_run=False, clock=lambda: NOW,
            maintenance=maintenance, **kwargs)
        self.addCleanup(writer.stop)
        return writer

    def test_startup_and_each_completed_turn_are_evaluated(self):
        maintenance = FakeMaintenance()
        writer = self.make_writer(maintenance)
        self.assertEqual(maintenance.calls, [None])

        for gen in (1, 2):
            self.assertTrue(writer.enqueue(make_job(gen=gen)))
        self.assertTrue(writer.wait_idle())

        self.assertEqual(maintenance.calls, [None, 1, 2])
        self.assertEqual(writer.counters().get("maintenance_runs", 0), 0)

    def test_counter_restores_from_state_and_trigger_resets_it(self):
        state = self.store.state
        state["turns_since_maintenance"] = 12
        self.store.save_state(state)
        maintenance = FakeMaintenance(trigger_at=15)
        writer = self.make_writer(maintenance)
        self.assertEqual(maintenance.calls, [None])

        for gen in (1, 2, 3, 4):
            self.assertTrue(writer.enqueue(make_job(gen=gen)))
        self.assertTrue(writer.wait_idle())

        self.assertEqual(maintenance.calls, [None, 13, 14, 15, 1])
        self.assertEqual(writer.counters()["maintenance_runs"], 1)

    def test_maintenance_failure_does_not_block_writes(self):
        writer = self.make_writer(FakeMaintenance(fail=True))
        self.assertTrue(writer.enqueue(make_job()))
        self.assertTrue(writer.wait_idle())

        self.assertEqual(writer.counters()["written_jobs"], 1)
        self.assertGreaterEqual(writer.counters()["maintenance_errors"], 1)
        self.assertEqual([item["text"] for item in self.store.active_facts()],
                         ["用户叫小林"])

    def test_dry_run_never_calls_maintenance(self):
        maintenance = FakeMaintenance()
        writer = memory_writer.MemoryWriter(
            self.store, dry_run=True, clock=lambda: NOW,
            maintenance=maintenance)
        self.addCleanup(writer.stop)
        self.assertTrue(writer.enqueue(make_job()))
        self.assertTrue(writer.wait_idle())
        self.assertEqual(maintenance.calls, [])
        self.assertEqual(writer.counters()["dry_run_jobs"], 1)

    def test_read_only_store_never_calls_maintenance(self):
        self.store.read_only = True
        maintenance = FakeMaintenance()
        writer = self.make_writer(maintenance)
        self.assertEqual(maintenance.calls, [])

        self.assertTrue(writer.enqueue(make_job()))
        self.assertTrue(writer.wait_idle())
        self.assertEqual(maintenance.calls, [])
        self.assertEqual(writer.counters()["read_only_jobs"], 1)


class RealMaintenanceAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = memory_store.MemoryStore(self.tmp.name, clock=lambda: NOW)

    def make_writer(self):
        maintenance = memory_maintenance.MemoryMaintenance(
            self.store, clock=lambda: NOW)
        writer = memory_writer.MemoryWriter(
            self.store, dry_run=False, clock=lambda: NOW,
            maintenance=maintenance)
        self.addCleanup(writer.stop)
        return writer

    def test_startup_maintenance_promotes_preference_and_records_state(self):
        episode = self.store.upsert_episode(
            "preference", "用户喜欢喝咖啡", slot="drink", now=NOW)
        for _ in range(3):
            self.store.mark_used(episodes=[episode["id"]], now=NOW)

        writer = self.make_writer()

        self.assertEqual([item["text"] for item in self.store.active_facts()],
                         ["用户喜欢喝咖啡"])
        self.assertEqual(self.store.active_episodes(), [])
        state = self.store.state
        self.assertEqual(state["last_maintenance"],
                         "2026-10-23T02:00:00+00:00")
        self.assertEqual(state["turns_since_maintenance"], 0)
        self.assertEqual(writer.counters()["maintenance_runs"], 1)

    def test_fifty_written_turns_trigger_maintenance_again(self):
        writer = self.make_writer()
        self.assertEqual(writer.counters()["maintenance_runs"], 1)

        for gen in range(1, 51):
            self.assertTrue(writer.enqueue(make_job(gen=gen)))
        self.assertTrue(writer.wait_idle(timeout=10))

        self.assertEqual(writer.counters()["maintenance_runs"], 2)
        state = self.store.state
        self.assertEqual(state["turns_since_maintenance"], 0)


class WriteJobTest(unittest.TestCase):
    def test_job_snapshots_turn_and_is_immutable(self):
        class FakeTurn:
            session_id = "dev1"
            user_text = "我叫小林"
            reply_text = "知道啦。"
            gen = 5
            completed = True
            error = None
            selected_ids = ["a", "b"]
            stats = {"done": True, "finish_reason": "stop"}

            def __init__(self):
                self.abort = threading.Event()

        job = memory_writer.WriteJob.from_turn(FakeTurn(), now=NOW)
        self.assertEqual(job.session_id, "dev1")
        self.assertEqual(job.user, "我叫小林")
        self.assertEqual(job.assistant, "知道啦。")
        self.assertEqual(job.gen, 5)
        self.assertTrue(job.done)
        self.assertEqual(job.finish_reason, "stop")
        self.assertEqual(job.selected_ids, ("a", "b"))
        self.assertFalse(job.aborted)
        self.assertEqual(job.ts, "2026-10-23T02:00:00+00:00")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            job.gen = 6


if __name__ == "__main__":
    unittest.main(verbosity=2)
