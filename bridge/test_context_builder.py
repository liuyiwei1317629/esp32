import copy
import unittest

from context_builder import (
    ASSISTANT_MAX_CHARS,
    CURRENT_INPUT_MAX_CHARS,
    HISTORY_TURNS,
    MEMORY_MAX_CHARS,
    MEMORY_MAX_ITEMS,
    PERSONA_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    USER_MAX_CHARS,
    appended_history,
    build_messages,
    clip_text,
    estimate_tokens,
    select_memories,
    trim_history,
)


def sections(content):
    return content.split("\n\n")


def system(messages):
    return messages[0]["content"]


def memory_lines(content):
    for part in sections(content):
        lines = part.split("\n")
        if lines[0] == "长期记忆：":
            return lines[1:]
    return []


def summary_part(content):
    for part in sections(content):
        if part.startswith("之前聊到："):
            return part[len("之前聊到："):]
    return None


def turn(user, assistant):
    return {"user": user, "assistant": assistant}


class BudgetConstantsTest(unittest.TestCase):
    def test_budget_numbers_are_product_constraints(self):
        self.assertEqual(PERSONA_MAX_CHARS, 120)
        self.assertEqual(MEMORY_MAX_ITEMS, 3)
        self.assertEqual(MEMORY_MAX_CHARS, 30)
        self.assertEqual(SUMMARY_MAX_CHARS, 100)
        self.assertEqual(HISTORY_TURNS, 2)
        self.assertEqual(USER_MAX_CHARS, 120)
        self.assertEqual(ASSISTANT_MAX_CHARS, 120)
        self.assertEqual(CURRENT_INPUT_MAX_CHARS, 500)


class ClipTextTest(unittest.TestCase):
    def test_truncates_to_limit(self):
        self.assertEqual(clip_text("一二三四五", 3), "一二三")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(clip_text("  你好  ", 10), "你好")

    def test_strips_trailing_whitespace_left_by_cut(self):
        self.assertEqual(clip_text("ab cd ef", 6), "ab cd")

    def test_none_is_empty(self):
        self.assertEqual(clip_text(None, 10), "")

    def test_non_positive_limit_is_empty(self):
        self.assertEqual(clip_text("你好", 0), "")
        self.assertEqual(clip_text("你好", -5), "")

    def test_adds_no_ellipsis(self):
        clipped = clip_text("一二三四五", 3)
        self.assertNotIn("…", clipped)
        self.assertFalse(clipped.endswith("..."))
        self.assertEqual(len(clipped), 3)

    def test_accepts_non_string_input(self):
        self.assertEqual(clip_text(12345, 3), "123")


class TrimHistoryTest(unittest.TestCase):
    def test_keeps_only_last_two_turns(self):
        history = [turn("u1", "a1"), turn("u2", "a2"), turn("u3", "a3")]
        self.assertEqual(trim_history(history), [turn("u2", "a2"), turn("u3", "a3")])

    def test_respects_explicit_max_turns(self):
        history = [turn("u1", "a1"), turn("u2", "a2"), turn("u3", "a3")]
        self.assertEqual(trim_history(history, 1), [turn("u3", "a3")])

    def test_non_positive_max_turns_is_empty(self):
        history = [turn("u1", "a1")]
        self.assertEqual(trim_history(history, 0), [])
        self.assertEqual(trim_history(history, -1), [])

    def test_none_history_is_empty(self):
        self.assertEqual(trim_history(None), [])

    def test_filters_invalid_items(self):
        history = [
            "not a dict",
            None,
            {"user": "只有用户"},
            {"user": "u", "assistant": None},
            {"user": 42, "assistant": "a"},
            {"user": "   ", "assistant": "a"},
            {"user": "u", "assistant": "\t\n"},
            turn("u ok", "a ok"),
        ]
        self.assertEqual(trim_history(history), [turn("u ok", "a ok")])

    def test_does_not_mutate_input(self):
        history = [turn("u1", "a1"), turn("u2", "a2")]
        snapshot = copy.deepcopy(history)
        result = trim_history(history)
        self.assertEqual(history, snapshot)
        self.assertIsNot(result, history)
        self.assertIsNot(result[0], history[0])

    def test_does_not_clip_here(self):
        long = "长" * 300
        result = trim_history([turn(long, long)])
        self.assertEqual(result, [turn(long, long)])


class AppendedHistoryTest(unittest.TestCase):
    def test_appends_and_keeps_last_two(self):
        history = [turn("u1", "a1"), turn("u2", "a2")]
        result = appended_history(history, "u3", "a3")
        self.assertEqual(result, [turn("u2", "a2"), turn("u3", "a3")])

    def test_strips_appended_values(self):
        result = appended_history([], "  u  ", "  a  ")
        self.assertEqual(result, [turn("u", "a")])

    def test_empty_user_is_not_appended(self):
        history = [turn("u1", "a1")]
        self.assertEqual(appended_history(history, "   ", "a2"), [turn("u1", "a1")])

    def test_empty_assistant_is_not_appended(self):
        history = [turn("u1", "a1")]
        self.assertEqual(appended_history(history, "u2", ""), [turn("u1", "a1")])

    def test_non_string_values_are_not_appended(self):
        history = [turn("u1", "a1")]
        self.assertEqual(appended_history(history, None, "a2"), [turn("u1", "a1")])
        self.assertEqual(appended_history(history, "u2", 7), [turn("u1", "a1")])

    def test_invalid_old_turns_are_dropped(self):
        history = [{"user": "bad"}, turn("u1", "a1")]
        self.assertEqual(appended_history(history, "u2", "a2"), [turn("u1", "a1"), turn("u2", "a2")])

    def test_does_not_mutate_input(self):
        history = [turn("u1", "a1")]
        snapshot = copy.deepcopy(history)
        appended_history(history, "u2", "a2")
        self.assertEqual(history, snapshot)


class SelectMemoriesTest(unittest.TestCase):
    def test_returns_at_most_three(self):
        memories = ["天气" + "甲" * 20, "天气" + "乙" * 20, "天气" + "丙" * 20, "天气" + "丁" * 20]
        chosen = select_memories("今天天气如何", memories)
        self.assertEqual(len(chosen), MEMORY_MAX_ITEMS)
        self.assertTrue(all(m.startswith("天气") for m in chosen))

    def test_excludes_zero_score(self):
        self.assertEqual(select_memories("天气", ["完全无关的内容"]), [])

    def test_prefers_higher_overlap(self):
        memories = ["天气", "今天天气很好"]
        self.assertEqual(select_memories("今天天气", memories), ["今天天气很好", "天气"])

    def test_ties_prefer_newer_entry(self):
        memories = ["甲乙丙", "甲乙丁", "甲乙戊"]
        self.assertEqual(select_memories("甲乙", memories), ["甲乙戊", "甲乙丁", "甲乙丙"])

    def test_accepts_plain_strings(self):
        self.assertEqual(select_memories("小名", ["它的小名是豆豆"]), ["它的小名是豆豆"])

    def test_accepts_dicts_with_text(self):
        memories = [{"text": "它喜欢雨", "tag": "x"}, {"text": "无关内容"}]
        self.assertEqual(select_memories("喜欢雨", memories), ["它喜欢雨"])

    def test_skips_blank_and_non_string_entries(self):
        memories = [None, 42, "   ", {"text": "  "}, {"nope": "匹配"}, "匹配"]
        self.assertEqual(select_memories("匹配", memories), ["匹配"])

    def test_empty_query_returns_nothing(self):
        memories = ["甲", "乙"]
        self.assertEqual(select_memories("", memories), [])
        self.assertEqual(select_memories("   ", memories), [])
        self.assertEqual(select_memories(None, memories), [])

    def test_non_positive_k_returns_nothing(self):
        self.assertEqual(select_memories("甲", ["甲"], 0), [])
        self.assertEqual(select_memories("甲", ["甲"], -1), [])

    def test_none_memories_returns_nothing(self):
        self.assertEqual(select_memories("甲", None), [])

    def test_returns_stripped_raw_text_without_clipping(self):
        long = "天气" + "记" * 60
        chosen = select_memories("天气", ["  " + long + "  "])
        self.assertEqual(chosen, [long])

    def test_punctuation_and_whitespace_do_not_affect_matching(self):
        self.assertEqual(select_memories("今天天气", ["今天，天气不错"]), ["今天，天气不错"])
        self.assertEqual(select_memories("今天 天气", ["今天天气不错"]), ["今天天气不错"])
        self.assertEqual(select_memories("今天，天气。", ["今天天气不错"]), ["今天天气不错"])

    def test_fullwidth_latin_is_normalized(self):
        self.assertEqual(select_memories("说 ＨＥＬＬＯ 了", ["hello world"]), ["hello world"])

    def test_single_cjk_char_query_matches_single_char_run(self):
        self.assertEqual(select_memories("猫", ["猫"]), ["猫"])

    def test_latin_and_digit_tokens_split_on_non_alphanumeric(self):
        self.assertEqual(select_memories("order-42", ["订单 order 42 号"]), ["订单 order 42 号"])

    def test_does_not_mutate_memories(self):
        memories = [{"text": "天气"}, "天气"]
        snapshot = copy.deepcopy(memories)
        select_memories("天气", memories)
        self.assertEqual(memories, snapshot)


class BuildMessagesTest(unittest.TestCase):
    def test_message_order_is_system_history_then_current(self):
        messages = build_messages(
            persona="测试人格",
            history=[turn("u1", "a1"), turn("u2", "a2")],
            user_text="u3",
        )
        self.assertEqual(
            [m["role"] for m in messages],
            ["system", "user", "assistant", "user", "assistant", "user"],
        )
        self.assertEqual(messages[-1]["content"], "u3")

    def test_no_history_only_system_and_current(self):
        messages = build_messages(persona="测试人格", history=[], user_text="你好")
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(system(messages), "测试人格")

    def test_clips_persona(self):
        messages = build_messages(persona="人" * 300, history=[], user_text="你好")
        self.assertEqual(system(messages), "人" * PERSONA_MAX_CHARS)

    def test_clips_memories_and_keeps_three(self):
        memories = ["天气" + "甲" * 60, "天气" + "乙" * 60, "天气" + "丙" * 60, "天气" + "丁" * 60]
        messages = build_messages("测试人格", [], "今天天气", memories=memories)
        lines = memory_lines(system(messages))
        self.assertEqual(len(lines), MEMORY_MAX_ITEMS)
        for line in lines:
            self.assertTrue(line.startswith("- "))
            self.assertEqual(len(line), 2 + MEMORY_MAX_CHARS)

    def test_clips_summary(self):
        messages = build_messages("测试人格", [], "你好", summary="前情" * 200)
        self.assertEqual(summary_part(system(messages)), "前情" * (SUMMARY_MAX_CHARS // 2))

    def test_section_order_is_persona_memories_summary(self):
        messages = build_messages(
            "测试人格", [], "今天天气", memories=["天气"], summary="之前聊过天气"
        )
        parts = sections(system(messages))
        self.assertEqual(parts[0], "测试人格")
        self.assertTrue(parts[1].startswith("长期记忆："))
        self.assertTrue(parts[2].startswith("之前聊到："))

    def test_no_memory_section_when_nothing_matches(self):
        messages = build_messages("测试人格", [], "你好", memories=["完全无关"])
        self.assertNotIn("长期记忆：", system(messages))

    def test_no_summary_section_when_summary_blank(self):
        messages = build_messages("测试人格", [], "你好", summary="   ")
        self.assertIsNone(summary_part(system(messages)))

    def test_clips_history_assistant_and_user(self):
        history = [turn("问" * 300, "答" * 300)]
        messages = build_messages("测试人格", history, "你好")
        self.assertEqual(messages[1]["content"], "问" * USER_MAX_CHARS)
        self.assertEqual(messages[2]["content"], "答" * ASSISTANT_MAX_CHARS)

    def test_keeps_only_last_two_history_turns(self):
        history = [turn("u1", "a1"), turn("u2", "a2"), turn("u3", "a3")]
        messages = build_messages("测试人格", history, "u4")
        self.assertEqual(
            [m["role"] for m in messages],
            ["system", "user", "assistant", "user", "assistant", "user"],
        )
        self.assertEqual(messages[1]["content"], "u2")

    def test_current_input_is_clipped(self):
        messages = build_messages("测试人格", [], "语" * 700)
        self.assertEqual(messages[-1]["content"], "语" * CURRENT_INPUT_MAX_CHARS)

    def test_current_input_is_not_in_history(self):
        user_text = "今天天气如何"
        messages = build_messages("测试人格", [turn("u1", "a1")], user_text)
        self.assertEqual(
            [m["content"] for m in messages].count(user_text), 1
        )
        self.assertEqual(messages[-1]["role"], "user")

    def test_rejects_blank_persona(self):
        with self.assertRaises(ValueError):
            build_messages("   ", [], "你好")

    def test_rejects_blank_user_text(self):
        with self.assertRaises(ValueError):
            build_messages("测试人格", [], "  \n ")

    def test_rejects_missing_persona_and_user_text(self):
        with self.assertRaises(ValueError):
            build_messages(None, [], None)

    def test_does_not_mutate_inputs(self):
        persona = "测试人格"
        history = [turn("u1", "a1"), {"user": "bad"}]
        memories = ["天气" + "甲" * 60]
        summary = "摘要" * 100
        snapshot = (copy.deepcopy(history), copy.deepcopy(memories), summary)
        build_messages(persona, history, "今天天气", memories=memories, summary=summary)
        self.assertEqual((history, memories, summary), snapshot)

    def test_every_message_has_content(self):
        messages = build_messages(
            "测试人格",
            [turn("u1", "a1")],
            "你好",
            memories=["天气"],
            summary="之前聊过天气",
        )
        self.assertTrue(all(m["content"] for m in messages))

    def test_accepts_none_history_and_memories(self):
        messages = build_messages("测试人格", None, "你好", memories=None)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])


class EstimateTokensTest(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("   \n "), 0)
        self.assertEqual(estimate_tokens(None), 0)

    def test_cjk_grows_one_per_char(self):
        self.assertEqual(estimate_tokens("中"), 1)
        self.assertEqual(estimate_tokens("中文"), 2)
        self.assertEqual(estimate_tokens("中文字"), 3)

    def test_latin_uses_four_chars_per_token_rounded_up(self):
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)

    def test_whitespace_is_not_counted(self):
        self.assertEqual(estimate_tokens("a b c"), 1)

    def test_mixed_text_sums_both_rules(self):
        self.assertEqual(estimate_tokens("中文 abcd"), 3)

    def test_extension_ideographs_and_ideographic_zero_are_cjk(self):
        self.assertEqual(estimate_tokens("𠮷" * 4), 4)
        self.assertEqual(estimate_tokens("〇"), 1)


class TokenBudgetTest(unittest.TestCase):
    def test_realistic_round_fits_budget(self):
        persona = "小猫设定" * 15
        history = [turn("你今天开心吗" * 2, "我很开心呀" * 4) for _ in range(2)]
        memories = ["天气" + "甲" * 28, "天气" + "乙" * 28, "天气" + "丙" * 28]
        messages = build_messages(
            persona, history, "讲讲今天的天气", memories, "前情摘要" * 25
        )
        total = sum(estimate_tokens(m["content"]) for m in messages)
        self.assertEqual(len(messages), 6)
        self.assertLessEqual(total, 530)


if __name__ == "__main__":
    unittest.main()
