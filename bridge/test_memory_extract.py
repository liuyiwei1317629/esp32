import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_extract

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 23, 1, 0, tzinfo=CST)  # Friday


def one(text, **kwargs):
    items = memory_extract.extract(text, now=kwargs.pop("now", NOW))
    return items[0] if items else None


class SensitiveTest(unittest.TestCase):
    def test_detects_common_secret_patterns(self):
        samples = {
            "phone": "我手机号是13812345678",
            "id_card": "身份证号110101199001011234",
            "bank_card": "卡号6222021234567890123",
            "verification_code": "验证码是123456",
            "password": "我的密码是 abc12345",
            "api_key": "token=abcdef1234567890",
            "address": "我住在幸福路12号3单元501",
        }
        for label, text in samples.items():
            with self.subTest(label=label):
                self.assertIn(label, memory_extract.sensitive_reasons(text),
                              memory_extract.sensitive_reasons(text))
                self.assertTrue(memory_extract.has_sensitive(text))

    def test_normal_sentences_are_not_sensitive(self):
        for text in ("我明天要交设计稿", "我喜欢香菜", "我住在北京",
                     "我今年二十八岁", "今天天气不错"):
            self.assertFalse(memory_extract.has_sensitive(text), text)

    def test_sensitive_text_extracts_nothing(self):
        self.assertEqual(memory_extract.extract("记住我的手机号13812345678",
                                                now=NOW), [])


class FillerTest(unittest.TestCase):
    def test_strips_leading_fillers_only(self):
        self.assertEqual(memory_extract.strip_fillers("嗯，呃，那个我喜欢香菜"),
                         "我喜欢香菜")
        self.assertEqual(memory_extract.strip_fillers("我喜欢那个杯子"),
                         "我喜欢那个杯子")

    def test_clean_intent_and_first_sentence(self):
        self.assertEqual(memory_extract.clean_intent("嗯，今天有点累。"), "今天有点累。")
        self.assertEqual(memory_extract.first_sentence("好呀。今天早点睡！"),
                         "好呀")
        self.assertEqual(memory_extract.first_sentence("没有标点"), "没有标点")
        self.assertEqual(len(memory_extract.first_sentence("长" * 50)), 28)


class DateTest(unittest.TestCase):
    def test_relative_days_anchor_at_local_midnight(self):
        cases = {
            "今天": NOW.date(),
            "明天": NOW.date() + timedelta(days=1),
            "后天": NOW.date() + timedelta(days=2),
            "大后天": NOW.date() + timedelta(days=3),
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                due = memory_extract.resolve_due(phrase, now=NOW)
                self.assertIsNotNone(due)
                local = due.astimezone(CST)
                self.assertEqual(local.date(), expected)
                self.assertEqual((local.hour, local.minute), (0, 0))

    def test_next_week_monday(self):
        due = memory_extract.resolve_due("下周一", now=NOW)
        self.assertIsNotNone(due)
        local = due.astimezone(CST)
        self.assertEqual(local.weekday(), 0)
        self.assertEqual(local.date(), datetime(2026, 10, 26).date())

    def test_next_week_sunday(self):
        due = memory_extract.resolve_due("下周日", now=NOW)
        self.assertIsNotNone(due)
        self.assertEqual(due.astimezone(CST).date(), datetime(2026, 11, 1).date())

    def test_absolute_month_day(self):
        due = memory_extract.resolve_due("11月5日", now=NOW)
        self.assertIsNotNone(due)
        self.assertEqual(due.astimezone(CST).date(), datetime(2026, 11, 5).date())

    def test_unparseable_is_none(self):
        for phrase in ("", "过几天", "月底", "下周八", "13月1日"):
            self.assertIsNone(memory_extract.resolve_due(phrase, now=NOW), phrase)


class NameTest(unittest.TestCase):
    def test_name_patterns(self):
        for text, expected in (
            ("我叫小林", "用户叫小林"),
            ("我的名字是阿杰", "用户叫阿杰"),
            ("你可以叫我豆豆", "用户叫豆豆"),
            ("叫我小王", "用户叫小王"),
        ):
            with self.subTest(text=text):
                item = one(text)
                self.assertIsNotNone(item)
                self.assertEqual(item["kind"], "name")
                self.assertEqual(item["slot"], "name")
                self.assertEqual(item["text"], expected)

    def test_bad_or_empty_name_is_skipped(self):
        for text in ("我叫什么", "我叫", "我叫谁"):
            self.assertEqual(memory_extract.extract(text, now=NOW), [], text)


class PreferenceTest(unittest.TestCase):
    def test_positive_and_negative_keep_polarity(self):
        positive = one("我喜欢香菜")
        self.assertEqual(positive["kind"], "preference")
        self.assertEqual(positive["text"], "用户喜欢香菜")
        self.assertEqual(positive["slot"], "pref:香菜")
        self.assertEqual(positive["polarity"], "positive")

        negative = one("我不喜欢香菜")
        self.assertEqual(negative["text"], "用户不喜欢香菜")
        self.assertEqual(negative["polarity"], "negative")

        hates = one("我讨厌下雨天")
        self.assertEqual(hates["text"], "用户不喜欢下雨天")
        self.assertEqual(hates["polarity"], "negative")

    def test_question_and_hypothetical_are_skipped(self):
        for text in ("我喜欢什么", "我喜欢香菜吗", "如果我喜欢香菜呢",
                     "他说他喜欢香菜"):
            self.assertEqual(memory_extract.extract(text, now=NOW), [], text)


class FactPatternsTest(unittest.TestCase):
    def test_residence_work_school(self):
        residence = one("我住在北京")
        self.assertEqual((residence["kind"], residence["slot"],
                          residence["text"]),
                         ("residence", "city", "用户住在北京"))
        work = one("我在上海工作")
        self.assertEqual((work["kind"], work["slot"], work["text"]),
                         ("fact", "work", "用户在上海工作"))
        school = one("我在北京大学上学")
        self.assertEqual((school["kind"], school["slot"], school["text"]),
                         ("fact", "school", "用户在北京大学上学"))

    def test_relation_and_pet(self):
        relation = one("我的妈妈是张芳")
        self.assertEqual(relation["kind"], "relation")
        self.assertEqual(relation["slot"], "relation:妈妈")
        self.assertEqual(relation["text"], "用户的妈妈是张芳")
        pet = one("我养了一只猫叫豆豆")
        self.assertEqual(pet["kind"], "relation")
        self.assertEqual(pet["slot"], "pet:豆豆")
        self.assertEqual(pet["text"], "用户养了一只猫叫豆豆")

    def test_multiple_clauses_in_one_turn(self):
        items = memory_extract.extract("我叫小林，我喜欢香菜", now=NOW)
        self.assertEqual([item["kind"] for item in items], ["name", "preference"])


class TodoTest(unittest.TestCase):
    def test_todo_with_relative_date(self):
        item = one("我明天要交设计稿")
        self.assertEqual(item["kind"], "todo")
        self.assertEqual(item["text"], "用户明天要交设计稿")
        self.assertEqual(item["slot"], "todo:交设计稿")
        self.assertIsNotNone(item["due_at"])
        self.assertEqual(item["due_at"].astimezone(CST).date(),
                         datetime(2026, 10, 24).date())

    def test_remind_me_without_date_has_no_due(self):
        item = one("提醒我买牛奶")
        self.assertEqual(item["kind"], "todo")
        self.assertEqual(item["text"], "用户要买牛奶")
        self.assertIsNone(item["due_at"])

    def test_remind_me_with_date_extracts_due(self):
        item = one("提醒我明天买牛奶")
        self.assertEqual(item["when"], "明天")
        self.assertEqual(item["text"], "用户明天要买牛奶")
        self.assertIsNotNone(item["due_at"])

    def test_general_intention_without_time_is_not_todo(self):
        self.assertEqual(memory_extract.extract("我要去北京", now=NOW), [])

    def test_negative_reminder_is_not_todo(self):
        self.assertEqual(memory_extract.extract("别提醒我买牛奶", now=NOW), [])


class ExplicitTest(unittest.TestCase):
    def test_explicit_remember_is_pinned_fact(self):
        item = one("记住我下周三要交设计稿")
        self.assertEqual(item["kind"], "fact")
        self.assertTrue(item["pinned"])
        self.assertEqual(item["source"], "explicit")
        self.assertEqual(item["confidence"], memory_extract.EXPLICIT_CONFIDENCE)
        self.assertEqual(item["text"], "用户下周三要交设计稿")

    def test_other_explicit_phrases(self):
        for text in ("记一下我的生日是5月1日", "帮我记住我喜欢香菜",
                     "别忘了带钥匙"):
            item = one(text)
            self.assertIsNotNone(item, text)
            self.assertTrue(item["pinned"], text)

    def test_explicit_question_is_skipped(self):
        self.assertEqual(memory_extract.extract("记住我喜欢什么了吗", now=NOW), [])


class ThirdPersonTest(unittest.TestCase):
    def test_third_person_and_reported_speech_are_skipped(self):
        for text in ("他喜欢香菜", "她说她住在北京", "朋友说明天要交稿"):
            self.assertEqual(memory_extract.extract(text, now=NOW), [], text)


if __name__ == "__main__":
    unittest.main()
