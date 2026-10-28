import unittest

import emotion


class TagTests(unittest.TestCase):
    def test_empty_is_calm(self):
        self.assertEqual(emotion.tag(""), emotion.CALM)
        self.assertEqual(emotion.tag(None), emotion.CALM)

    def test_plain_text_is_calm(self):
        self.assertEqual(emotion.tag("今天天气还行，要不要出去走走"), emotion.CALM)

    def test_excited_wins_over_plain_joy(self):
        self.assertEqual(emotion.tag("太棒了，我迫不及待想试试"), "excited")

    def test_happy(self):
        self.assertEqual(emotion.tag("哈哈，真好，谢谢你"), "happy")

    def test_sad(self):
        self.assertEqual(emotion.tag("我有点难过，想哭"), "sad")

    def test_angry(self):
        self.assertEqual(emotion.tag("我生气了，别烦我"), "angry")

    def test_surprised(self):
        self.assertEqual(emotion.tag("真的吗？不会吧"), "surprised")

    def test_shy(self):
        self.assertEqual(emotion.tag("有点害羞，别看我"), "shy")

    def test_longer_keyword_beats_overlapping_short_one(self):
        self.assertEqual(emotion.tag("讨厌啦"), "shy")     # 「讨厌啦」不再算「讨厌」
        self.assertEqual(emotion.tag("你讨厌"), "angry")

    def test_more_hits_win(self):
        self.assertEqual(emotion.tag("难过，真的很难过，想哭"), "sad")

    def test_tie_goes_to_earliest(self):
        self.assertEqual(emotion.tag("哈哈，不过我有点难过"), "happy")
        self.assertEqual(emotion.tag("我有点难过，哈哈"), "sad")

    def test_fullwidth_is_normalized(self):
        self.assertEqual(emotion.tag("好开心！！"), "happy")

    def test_result_is_always_known_tag(self):
        for text in ("", "嗯", "好", "太棒", "滚", "嘻嘻嘻", "呜呜"):
            self.assertIn(emotion.tag(text), emotion.EMOTIONS + (emotion.CALM,))


if __name__ == "__main__":
    unittest.main()
