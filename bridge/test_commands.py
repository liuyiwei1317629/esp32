import dataclasses
import unittest

from commands import Command, parse_command


class StopTests(unittest.TestCase):
    PHRASES = [
        "停",
        "停停",
        "停一下",
        "停一停",
        "停下来",
        "停止",
        "别说了",
        "别讲了",
        "别念了",
        "不要说了",
        "别吵了",
        "闭嘴",
        "安静",
        "安静点",
        "打断",
    ]

    def test_all_stop_phrases(self):
        self.assertEqual(len(self.PHRASES), 15)
        for phrase in self.PHRASES:
            with self.subTest(phrase=phrase):
                command = parse_command(phrase)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "stop")

    def test_stop_with_trailing_particles(self):
        cases = [
            "停吧",
            "停啊",
            "停呀",
            "停呢",
            "停一停吧",
            "停下来啊",
            "停一下呀",
            "停止呢",
            "别说了吧",
            "别讲了呀",
            "不要说了啊",
            "安静点呀",
            "闭嘴吧",
            "打断吧",
            "别说了好吗",
            "停下来行吗",
        ]
        for text in cases:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "stop")

    def test_stop_with_trailing_punctuation(self):
        for text in ["停。", "停！", "停？", "停~", "停～", "停…", "停，", "别说了！"]:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "stop")

    def test_stop_fullwidth_punctuation(self):
        for text in ["停！", "安静？", "别说了。"]:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "stop")

    def test_stop_rejects_partial_sentences(self):
        for text in ["我停不下来", "停在哪里", "别说了之后呢", "他叫我停下来", "安静的房间"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_stop_rejects_middle_punctuation(self):
        for text in ["停，一下", "安静，点"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_stop_rejects_long_sentence(self):
        text = "麻烦你现在立刻马上停下来不要动了"
        self.assertGreater(len(text), 12)
        self.assertIsNone(parse_command(text))

    def test_stop_returns_defaults(self):
        command = parse_command("停")
        self.assertEqual(command.kind, "stop")
        self.assertIsNone(command.seconds)
        self.assertEqual(command.label, "")
        self.assertEqual(command.raw, "停")

    def test_stop_ignores_surrounding_whitespace(self):
        command = parse_command("  停下来。  ")
        self.assertEqual(command.kind, "stop")
        self.assertEqual(command.raw, "停下来。")


class TimeTests(unittest.TestCase):
    PHRASES = [
        "现在几点",
        "现在几点了",
        "现在几点钟",
        "几点了",
        "几点",
        "现在时间",
        "现在是什么时间",
        "什么时间",
        "报时",
    ]

    def test_all_time_phrases(self):
        self.assertEqual(len(self.PHRASES), 9)
        for phrase in self.PHRASES:
            with self.subTest(phrase=phrase):
                command = parse_command(phrase)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "time")

    def test_time_with_trailing_particles(self):
        for text in ["现在几点啊", "几点了呀", "报时吗", "现在时间呢", "什么时间啊", "现在是什么时间呀"]:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "time")

    def test_time_with_trailing_punctuation(self):
        for text in ["现在几点？", "现在几点了。", "报时！"]:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "time")

    def test_time_rejects_negatives(self):
        for text in ["你几点睡", "现在三点", "三点", "我几点来"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_time_does_not_match_extra_words(self):
        for text in ["我想知道现在几点", "现在几点钟了"]:
            with self.subTest(text=text):
                command = parse_command(text)
                if text == "现在几点钟了":
                    self.assertIsNotNone(command)
                    self.assertEqual(command.kind, "time")
                else:
                    self.assertIsNone(command)

    def test_time_returns_defaults(self):
        command = parse_command("现在几点")
        self.assertEqual(command.kind, "time")
        self.assertIsNone(command.seconds)
        self.assertEqual(command.label, "")
        self.assertEqual(command.raw, "现在几点")


class TimerTests(unittest.TestCase):
    def test_timer_spec_examples(self):
        cases = [
            ("提醒我五分钟后喝水", 300, "喝水"),
            ("五分钟后提醒我喝水", 300, "喝水"),
            ("定个三分钟计时", 180, ""),
            ("半小时后叫我", 1800, ""),
            ("一小时后提醒我看锅", 3600, "看锅"),
            ("倒计时90秒", 90, ""),
            ("一个半小时后提醒我关火", 5400, "关火"),
            ("一刻钟后叫我", 900, ""),
            ("两分钟后提醒我关灯", 120, "关灯"),
            ("1.5小时后提醒我收衣服", 5400, "收衣服"),
        ]
        for text, seconds, label in cases:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "timer")
                self.assertEqual(command.seconds, seconds)
                self.assertEqual(command.label, label)

    def test_timer_arabic_numbers(self):
        cases = [
            ("提醒我5分钟后喝水", 300),
            ("倒计时30秒", 30),
            ("定时90秒钟", 90),
            ("计时1小时", 3600),
            ("提醒我59秒后", 59),
        ]
        for text, seconds in cases:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "timer")
                self.assertEqual(command.seconds, seconds)

    def test_timer_chinese_numbers(self):
        cases = [
            ("计时十秒", 10),
            ("提醒我十一分钟后", 660),
            ("计时二十秒钟", 20),
            ("提醒我二十三分钟后", 1380),
            ("计时九十九秒", 99),
            ("提醒我三十分钟后", 1800),
        ]
        for text, seconds in cases:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "timer")
                self.assertEqual(command.seconds, seconds)

    def test_timer_liang_is_two(self):
        command = parse_command("提醒我两分钟后关灯")
        self.assertEqual(command.kind, "timer")
        self.assertEqual(command.seconds, 120)

    def test_timer_half_units(self):
        cases = [
            ("半小时后叫我", 1800),
            ("提醒我半分钟后", 30),
            ("计时半钟头", 1800),
        ]
        for text, seconds in cases:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.seconds, seconds)

    def test_timer_one_and_a_half_hours(self):
        for text in ["一个半小时后提醒我关火", "一个半钟头后提醒我关火"]:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.seconds, 5400)

    def test_timer_quarter_hour(self):
        command = parse_command("一刻钟后叫我")
        self.assertEqual(command.kind, "timer")
        self.assertEqual(command.seconds, 900)

    def test_timer_decimal(self):
        cases = [
            ("1.5小时后提醒我收衣服", 5400),
            ("提醒我0.5分钟后", 30),
        ]
        for text, seconds in cases:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.seconds, seconds)

    def test_timer_all_units(self):
        cases = [
            ("计时5秒", 5),
            ("计时5秒钟", 5),
            ("计时5分", 300),
            ("计时5分钟", 300),
            ("计时5小时", 18000),
            ("计时5钟头", 18000),
        ]
        for text, seconds in cases:
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertIsNotNone(command)
                self.assertEqual(command.seconds, seconds)

    def test_timer_max_24_hours(self):
        command = parse_command("计时24小时")
        self.assertEqual(command.kind, "timer")
        self.assertEqual(command.seconds, 86400)

    def test_timer_rejects_no_keyword(self):
        self.assertIsNone(parse_command("我三分钟后到"))

    def test_timer_rejects_no_duration(self):
        for text in ["提醒我", "计时", "倒计时", "定时", "闹钟"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_zero(self):
        for text in ["定个0分钟", "计时0秒", "提醒我0.0小时后"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_over_24_hours(self):
        for text in ["计时999小时", "提醒我25小时后"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_half_second(self):
        for text in ["提醒我半秒后", "计时半秒钟"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_negation(self):
        cases = [
            "别提醒我五分钟后喝水",
            "不要定时三分钟",
            "不用提醒我五分钟",
            "别计时五分钟",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_negated_call_me(self):
        for text in ["五分钟后别叫我", "五分钟后不要叫我", "五分钟后不用叫我"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_cancel(self):
        for text in ["取消计时", "取消三分钟计时", "取消闹钟", "取消提醒"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_turn_off(self):
        for text in ["关掉闹钟", "关掉提醒", "关掉计时", "关掉五分钟闹钟"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_stop_countdown(self):
        for text in ["停止计时", "停止倒计时"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_compound_expressions(self):
        for text in ["1小时30分后提醒我", "提醒我一小时三十分钟"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_unsupported_chinese_magnitude(self):
        for text in ["一百分钟后提醒我", "提醒我两百分钟", "计时一千秒"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_timer_rejects_unsupported_decimal_precision(self):
        self.assertIsNone(parse_command("提醒我1.55小时后"))

    def test_timer_does_not_false_reject_bie_wangle(self):
        command = parse_command("别忘了提醒我五分钟后喝水")
        self.assertIsNotNone(command)
        self.assertEqual(command.kind, "timer")
        self.assertEqual(command.seconds, 300)
        self.assertEqual(command.label, "喝水")

    def test_timer_seconds_is_int(self):
        command = parse_command("提醒我五分钟后喝水")
        self.assertIsInstance(command.seconds, int)

    def test_timer_returns_raw(self):
        command = parse_command(" 提醒我五分钟后喝水。 ")
        self.assertEqual(command.kind, "timer")
        self.assertEqual(command.raw, "提醒我五分钟后喝水。")


class LabelTests(unittest.TestCase):
    def test_label_prefix_reminder(self):
        cases = [
            ("提醒我五分钟后喝水", "喝水"),
            ("提醒我三分钟后关火", "关火"),
            ("提醒我十分钟后拿快递", "拿快递"),
        ]
        for text, label in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).label, label)

    def test_label_suffix_reminder(self):
        cases = [
            ("五分钟后提醒我喝水", "喝水"),
            ("一小时后提醒我看锅", "看锅"),
            ("两分钟后提醒我关灯", "关灯"),
            ("1.5小时后提醒我收衣服", "收衣服"),
            ("别忘了提醒我五分钟后喝水", "喝水"),
        ]
        for text, label in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).label, label)

    def test_label_empty_cases(self):
        for text in [
            "定个三分钟计时",
            "半小时后叫我",
            "一刻钟后叫我",
            "倒计时90秒",
            "一小时后提醒我",
            "计时五分钟",
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).label, "")

    def test_label_truncated_to_40(self):
        command = parse_command("提醒我五分钟后" + "喝水" * 25)
        self.assertEqual(command.label, "喝水" * 20)
        self.assertEqual(len(command.label), 40)

    def test_label_strips_edge_punctuation_and_space(self):
        cases = [
            ("提醒我五分钟后，喝水。", "喝水"),
            ("提醒我五分钟后 喝水", "喝水"),
            ("提醒我五分钟后（喝水）", "喝水"),
        ]
        for text, label in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).label, label)


class GeneralTests(unittest.TestCase):
    def test_empty_and_whitespace(self):
        for text in ["", "   ", "\t\n", "　", "。"]:
            with self.subTest(text=repr(text)):
                self.assertIsNone(parse_command(text))

    def test_unknown_returns_none(self):
        for text in ["你好", "打开灯", "我停不下来", "现在三点"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_command_is_frozen(self):
        command = parse_command("停")
        self.assertTrue(dataclasses.is_dataclass(command))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            command.kind = "time"

    def test_command_kind_is_limited(self):
        for text in ["停", "现在几点", "提醒我五分钟后喝水"]:
            with self.subTest(text=text):
                self.assertIn(parse_command(text).kind, {"stop", "time", "timer"})

    def test_raw_is_original_text_stripped(self):
        text = "  提醒我５分钟后喝水。 "
        command = parse_command(text)
        self.assertEqual(command.raw, "提醒我５分钟后喝水。")
        self.assertIn("５", command.raw)

    def test_nfkc_fullwidth_digits_equivalent(self):
        narrow = parse_command("提醒我5分钟后喝水")
        wide = parse_command("提醒我５分钟后喝水")
        self.assertEqual(narrow.kind, "timer")
        self.assertEqual(wide.kind, "timer")
        self.assertEqual(narrow.seconds, 300)
        self.assertEqual(wide.seconds, 300)
        self.assertEqual(narrow.label, wide.label)

    def test_repeated_calls_are_stable(self):
        text = "别忘了提醒我五分钟后喝水"
        first = parse_command(text)
        for _ in range(3):
            self.assertEqual(parse_command(text), first)

    def test_input_string_is_untouched(self):
        text = "  提醒我５分钟后喝水。 "
        parse_command(text)
        self.assertEqual(text, "  提醒我５分钟后喝水。 ")


if __name__ == "__main__":
    unittest.main()
