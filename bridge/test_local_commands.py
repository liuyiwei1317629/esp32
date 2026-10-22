import unittest
from datetime import datetime

import local_commands
from commands import Command, parse_command


class TimeReplyTests(unittest.TestCase):
    def test_morning(self):
        reply = local_commands.format_time_reply(datetime(2026, 10, 23, 9, 5))
        self.assertEqual(reply, "现在是上午九点零五分。")

    def test_afternoon_exact_hour(self):
        reply = local_commands.format_time_reply(datetime(2026, 10, 23, 14, 0))
        self.assertEqual(reply, "现在是下午二点整。")

    def test_noon(self):
        reply = local_commands.format_time_reply(datetime(2026, 10, 23, 12, 23))
        self.assertEqual(reply, "现在是中午十二点二十三分。")

    def test_midnight(self):
        reply = local_commands.format_time_reply(datetime(2026, 10, 23, 0, 30))
        self.assertEqual(reply, "现在是凌晨零点三十分。")

    def test_evening_single_digit_minute(self):
        reply = local_commands.format_time_reply(datetime(2026, 10, 23, 18, 1))
        self.assertEqual(reply, "现在是晚上六点零一分。")

    def test_late_evening(self):
        reply = local_commands.format_time_reply(datetime(2026, 10, 23, 23, 59))
        self.assertEqual(reply, "现在是晚上十一点五十九分。")

    def test_time_command_uses_now(self):
        command = parse_command("现在几点")
        reply = local_commands.reply_for(
            command, now=datetime(2026, 10, 23, 7, 0))
        self.assertEqual(reply, "现在是上午七点整。")


class TimerReplyTests(unittest.TestCase):
    def test_timer_with_label(self):
        command = parse_command("提醒我五分钟后喝水")
        reply = local_commands.reply_for(command)
        self.assertEqual(
            reply,
            "我知道你想在5分钟后提醒你喝水，但现在还不能到点主动出声。")

    def test_timer_without_label(self):
        command = parse_command("半小时后叫我")
        reply = local_commands.reply_for(command)
        self.assertEqual(
            reply,
            "我知道你想在30分钟后提醒你，但现在还不能到点主动出声。")

    def test_duration_hours_minutes_seconds(self):
        self.assertEqual(local_commands.format_duration(90), "1分钟30秒")
        self.assertEqual(local_commands.format_duration(3600), "1小时")
        self.assertEqual(local_commands.format_duration(5400), "1小时30分钟")
        self.assertEqual(local_commands.format_duration(86400), "24小时")


class StopReplyTests(unittest.TestCase):
    def test_stop(self):
        self.assertEqual(local_commands.reply_for(parse_command("停")), "好。")


class GeneralTests(unittest.TestCase):
    def test_none_and_unknown(self):
        self.assertIsNone(local_commands.reply_for(None))
        self.assertIsNone(local_commands.reply_for(Command(kind="unknown")))

    def test_plain_sentence_has_no_reply(self):
        self.assertIsNone(local_commands.reply_for(parse_command("今天天气不错")))


if __name__ == "__main__":
    unittest.main()
