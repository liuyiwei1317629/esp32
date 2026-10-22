import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_context
import memory_extract
import memory_store

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 23, 1, 0, tzinfo=CST)
PERSONA = "你是一个温和的陪伴者。" + "温" * 120


def fact(text, slot, pinned=False, used=None, hits=1, status="active"):
    return {
        "id": memory_store.record_id("fact", text),
        "kind": "fact",
        "text": text,
        "slot": slot,
        "pinned": pinned,
        "status": status,
        "revision": 1,
        "history": [],
        "created_at": NOW.isoformat(timespec="seconds"),
        "updated_at": NOW.isoformat(timespec="seconds"),
        "last_used_at": used or NOW.isoformat(timespec="seconds"),
        "hits": hits,
        "source": "rule",
    }


def episode(text, slot, kind="event", due=None, hits=0):
    item = fact(text, slot, used=NOW.isoformat(timespec="seconds"), hits=hits)
    item.update({
        "kind": kind,
        "due_at": due,
        "event_at": None,
        "fact_id": None,
    })
    return item


def turn(user, assistant, ts, gen=None):
    return {"user": user, "assistant": assistant, "ts": ts}


def system(messages):
    return messages[0]["content"]


class SummaryStorageTest(unittest.TestCase):
    def test_newest_first_and_total_budget(self):
        old = "旧" * 96
        stored = memory_context.build_summary_storage(
            "今天有点累", "今晚早点休息。", old, "下周交设计稿")
        summary = stored["summary"]
        sticky = stored["sticky_goal"]
        self.assertTrue(summary.startswith("今天有点累；今晚早点休息"))
        self.assertLessEqual(len(summary), 96)
        self.assertLessEqual(len(summary) + len(sticky), 100)
        self.assertEqual(sticky, "下周交设计稿")

    def test_ninety_six_char_old_summary_keeps_latest_sentences(self):
        stored = memory_context.build_summary_storage(
            "今天有点累", "今晚早点休息", "旧" * 96, "")
        injection = memory_context.build_summary_injection(
            stored["summary"], stored["sticky_goal"])
        self.assertIn("今天有点累", injection)
        self.assertIn("今晚早点休息", injection)
        self.assertLessEqual(len(injection), 80)

    def test_old_summary_is_trimmed_before_latest_sentences(self):
        stored = memory_context.build_summary_storage(
            "最新意图", "最新回复", "旧" * 96, "目标")
        injection = memory_context.build_summary_injection(
            stored["summary"], stored["sticky_goal"])
        self.assertIn("最新意图", injection)
        self.assertIn("最新回复", injection)
        self.assertIn("目标", injection)
        self.assertLessEqual(len(injection), 80)

    def test_injection_order_sticky_intent_reply_old(self):
        stored = memory_context.build_summary_storage(
            "意图", "回复", "旧摘要", "目标")
        injection = memory_context.build_summary_injection(
            stored["summary"], stored["sticky_goal"])
        self.assertEqual(injection, "目标｜意图｜回复｜旧摘要")

    def test_empty_summary_returns_empty(self):
        self.assertEqual(memory_context.build_summary_injection("", ""), "")


class MergeRecentTest(unittest.TestCase):
    def test_disk_and_cache_are_deduped_by_ts_user_assistant(self):
        disk = [turn("u1", "a1", "2026-10-23T00:00:00+00:00")]
        cache = [turn("u1", "a1", "2026-10-23T00:00:00+00:00"),
                 turn("u2", "a2", "2026-10-23T00:01:00+00:00")]
        merged = memory_context.merge_recent_turns(disk, cache)
        self.assertEqual([item["user"] for item in merged], ["u1", "u2"])

    def test_different_assistant_is_a_separate_turn(self):
        disk = [turn("u1", "a1", "2026-10-23T00:00:00+00:00")]
        cache = [turn("u1", "a2", "2026-10-23T00:00:00+00:00")]
        merged = memory_context.merge_recent_turns(disk, cache)
        self.assertEqual([item["assistant"] for item in merged], ["a1", "a2"])

    def test_keeps_last_two_and_clips(self):
        turns = [turn("u%d" % index, "a%d" % index,
                      "2026-10-23T00:0%d:00+00:00" % index, index)
                 for index in range(1, 4)]
        merged = memory_context.merge_recent_turns(turns, [], limit=2)
        self.assertEqual([item["user"] for item in merged], ["u2", "u3"])

    def test_limit_zero(self):
        self.assertEqual(memory_context.merge_recent_turns(
            [turn("u", "a", "t", 1)], [], limit=0), [])


class BuildMessagesTest(unittest.TestCase):
    def setUp(self):
        self.facts = [
            fact("用户叫小林", "name", pinned=True),
            fact("用户的妈妈是张芳", "relation:妈妈"),
            fact("用户住在北京", "city"),
        ]
        self.episodes = [
            episode("用户明天要交设计稿", "todo:设计稿", kind="todo",
                    due=(NOW + timedelta(hours=12)).isoformat(timespec="seconds")),
            episode("用户喜欢设计稿排版", "pref:设计稿", kind="preference"),
            episode("用户上周聊过设计稿", "event:设计稿"),
            episode("用户喜欢香菜", "pref:香菜", kind="preference"),
            episode("用户养了一只猫叫豆豆", "pet:豆豆", kind="relation"),
        ]
        self.summary = memory_context.build_summary_storage(
            "今天有点累", "今晚早点休息", "旧" * 40, "交设计稿")["summary"]
        self.sticky = "交设计稿"
        self.history = [
            turn("u" * 60, "a" * 70, "2026-10-23T00:00:00+00:00", 1),
            turn("今晚吃什么", "吃点清淡的", "2026-10-23T00:01:00+00:00", 2),
        ]

    def build(self, user="设计稿有点难", budget=1050, facts=None, episodes=None):
        return memory_context.build_messages(
            PERSONA, facts if facts is not None else self.facts,
            self.summary, self.sticky,
            episodes if episodes is not None else self.episodes,
            self.history, user, budget_tokens=budget, now=NOW,
        )

    def test_full_layers_fit_budget_and_keep_500_char_input(self):
        current = "设计稿" * 166 + "啊啊"
        self.assertEqual(len(current), 500)
        messages, info = self.build(user=current)
        self.assertLessEqual(info["total_tokens"], 1050)
        self.assertEqual(messages[-1]["content"], current)
        self.assertEqual(info["current_chars"], 500)
        self.assertEqual(info["l1"], 2)
        self.assertEqual(info["l2_chars"] > 0, True)
        self.assertGreaterEqual(info["l3"], 1)
        self.assertEqual(info["l4_turns"], 2)
        self.assertFalse(info["pinned_overflow"])
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("长期记忆：", system(messages))
        self.assertIn("之前聊到：", system(messages))

    def test_worst_case_layers_stay_within_1050(self):
        facts = [fact("事" * 30, "a", pinned=True),
                 fact("实" * 30, "b")]
        episodes = [episode("工作" + "一" * 28, "e1"),
                    episode("工作" + "二" * 28, "e2"),
                    episode("工作" + "三" * 28, "e3")]
        summary = memory_context.build_summary_storage(
            "意" * 28, "回" * 28, "旧" * 38, "目" * 20)["summary"]
        history = [turn("用" * 40, "答" * 50, "2026-10-23T00:00:00+00:00"),
                   turn("问" * 40, "回" * 50, "2026-10-23T00:01:00+00:00")]
        current = "工作" + "啊" * 498
        messages, info = memory_context.build_messages(
            PERSONA, facts, summary, "目" * 20, episodes, history, current,
            budget_tokens=1050, now=NOW)
        self.assertGreaterEqual(info["total_tokens"], 1000)
        self.assertLessEqual(info["total_tokens"], 1050)
        self.assertEqual(info["l1"], 2)
        self.assertEqual(info["l3"], 3)
        self.assertEqual(info["l4_turns"], 2)
        self.assertEqual(len(messages[-1]["content"]), 500)
        self.assertEqual(info["trimmed"], [])

    def test_current_input_is_not_silently_shortened_to_120(self):
        current = "设" * 500
        messages, info = self.build(user=current)
        self.assertEqual(len(messages[-1]["content"]), 500)
        self.assertEqual(info["current_chars"], 500)

    def test_hard_trim_order_l2_then_l3(self):
        current = "设计稿" * 166 + "啊啊"
        messages, info = self.build(user=current, budget=730)
        self.assertEqual(info["trimmed"][:3], ["L2:60", "L2:40", "L3:2"])
        self.assertNotIn("L4:drop_oldest", info["trimmed"])
        self.assertLessEqual(info["total_tokens"], 730)
        self.assertEqual(len(messages[-1]["content"]), 500)

    def test_tiny_budget_never_trims_persona_or_current_input(self):
        current = "设" * 500
        messages, info = memory_context.build_messages(
            "温" * 120, [], "", "", [], [], current,
            budget_tokens=200, now=NOW)
        self.assertEqual(messages[0]["content"], "温" * 120)
        self.assertEqual(len(messages[-1]["content"]), 500)
        self.assertGreater(info["total_tokens"], 200)
        self.assertIn("over_budget", info["warnings"])

    def test_pinned_more_than_two_reports_overflow_and_injects_two(self):
        facts = [fact("事实%d" % index, "slot%d" % index, pinned=True)
                 for index in range(3)]
        messages, info = self.build(facts=facts)
        self.assertTrue(info["pinned_overflow"])
        self.assertEqual(info["pinned_count"], 3)
        self.assertEqual(info["l1"], 2)
        self.assertEqual(system(messages).count("- "), 2 + info["l3"])

    def test_l3_zero_similarity_is_filtered(self):
        unrelated = [episode("完全无关的内容", "other")]
        messages, info = self.build(episodes=unrelated)
        self.assertEqual(info["l3"], 0)
        self.assertNotIn("完全无关的内容", system(messages))

    def test_l3_duplicate_text_is_not_counted_or_selected(self):
        duplicate = "用户喜欢香菜"
        facts = [fact(duplicate, "pref:香菜")]
        episodes = [episode(duplicate, "pref:香菜", kind="preference"),
                    episode("用户喜欢香菜和火锅", "pref:火锅",
                            kind="preference")]
        messages, info = self.build(user="用户喜欢香菜", facts=facts,
                                    episodes=episodes)
        self.assertEqual(info["l1"], 1)
        self.assertEqual(info["l3"], 1)
        self.assertEqual(info["selected_ids"],
                         [facts[0]["id"], episodes[1]["id"]])
        lines = system(messages).splitlines()
        self.assertEqual(lines.count("- " + duplicate), 1)
        self.assertIn("- 用户喜欢香菜和火锅", system(messages))

    def test_summary_is_injected_before_history_and_input(self):
        messages, _info = self.build()
        system_text = system(messages)
        self.assertIn("之前聊到：", system_text)
        self.assertIn("今晚早点休息", system_text)
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[-1]["role"], "user")

    def test_empty_layers_are_omitted(self):
        messages, info = memory_context.build_messages(
            "人格", [], "", "", [], [], "你好", now=NOW)
        self.assertEqual(system(messages), "人格")
        self.assertEqual(info["l1"], 0)
        self.assertEqual(info["l2_chars"], 0)
        self.assertEqual(info["l3"], 0)
        self.assertEqual([item["role"] for item in messages], ["system", "user"])

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            memory_context.build_messages("", [], "", "", [], [], "你好")
        with self.assertRaises(ValueError):
            memory_context.build_messages("人格", [], "", "", [], [], "  ")


class StoreBackedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = memory_store.MemoryStore(self.tmp.name, clock=lambda: NOW)

    def test_build_for_turn_reads_session_facts_episodes(self):
        self.store.upsert_fact("name", "用户叫小林", slot="name",
                               pinned=True, now=NOW)
        self.store.upsert_episode("todo", "用户今天要交设计稿",
                                  slot="todo:设计稿",
                                  due_at=NOW + timedelta(hours=2), now=NOW)
        self.store.save_session("dev1", {
            "summary": "最近在赶设计稿",
            "sticky_goal": "交设计稿",
            "recent_turns": [turn("用户", "回复", "2026-10-23T00:00:00+00:00", 7)],
            "completed_turns": 1,
        }, now=NOW)
        messages, info = memory_context.build_for_turn(
            self.store, "人格", "dev1", "设计稿", now=NOW)
        text = system(messages)
        self.assertFalse(info["fallback"])
        self.assertIn("用户叫小林", text)
        self.assertIn("用户今天要交设计稿", text)
        self.assertIn("最近在赶设计稿", text)
        self.assertEqual(messages[1]["content"], "用户")

    def test_build_for_turn_merges_unflushed_recent_cache(self):
        self.store.save_session("dev1", {
            "summary": "",
            "recent_turns": [turn("上一轮", "上一答",
                                  "2026-10-23T00:00:00+00:00")],
        }, now=NOW)
        messages, info = memory_context.build_for_turn(
            self.store, "人格", "dev1", "你好", now=NOW,
            recent_cache=[turn("刚完成", "刚回答",
                               "2026-10-23T00:01:00+00:00")])
        self.assertFalse(info["fallback"])
        self.assertEqual(messages[1]["content"], "上一轮")
        self.assertEqual(messages[3]["content"], "刚完成")

    def test_any_store_error_falls_back(self):
        class Broken:
            def active_facts(self):
                raise RuntimeError("boom")

        messages, info = memory_context.build_for_turn(
            Broken(), "人格", "dev1", "你好", now=NOW)
        self.assertTrue(info["fallback"])
        self.assertIn("boom", info["error"])
        self.assertEqual(system(messages), "人格")
        self.assertEqual(messages[-1]["content"], "你好")


if __name__ == "__main__":
    unittest.main()
