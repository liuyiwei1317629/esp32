import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import songs


class SongsTest(unittest.TestCase):
    def setUp(self):
        songs.reset()

    def test_playlist(self):
        items = songs.load()
        self.assertEqual([s["title"] for s in items],
                         ["The Window Stayed Open", "Let me go", "我会去拥抱他", "人是猫"])
        for song in items:
            size = os.path.getsize(song["path"])
            self.assertAlmostEqual(size / 32000.0, song["seconds"], delta=1.0)

    def test_pick_by_title(self):
        self.assertEqual(songs.pick("放一首《人是猫》")["title"], "人是猫")
        self.assertEqual(songs.pick("播放 let me go")["title"], "Let me go")
        self.assertEqual(songs.pick("我想听我会去拥抱他")["title"], "我会去拥抱他")
        self.assertEqual(songs.pick("放 The Window Stayed Open")["title"],
                         "The Window Stayed Open")
        self.assertEqual(songs.pick("人是猫")["title"], "人是猫")

    def test_generic_cycles(self):
        first = songs.pick("唱首歌")
        second = songs.pick("下一首")
        self.assertNotEqual(first["title"], second["title"])
        self.assertEqual(songs.pick("再放一遍")["title"], second["title"])

    def test_bare_command(self):
        songs.reset()
        self.assertIsNotNone(songs.pick("唱歌"))
        self.assertIsNotNone(songs.pick("听歌"))

    def test_no_false_positive(self):
        for text in ("你会唱歌吗", "人是猫吗", "今天天气怎么样",
                     "let me go to sleep", "我喜欢听歌"):
            self.assertIsNone(songs.pick(text), text)

    def test_prompt_block_lists_every_title(self):
        block = songs.prompt_block()
        for title in ("The Window Stayed Open", "Let me go", "我会去拥抱他", "人是猫"):
            self.assertIn("《%s》" % title, block)
        self.assertIn("原唱", block)
        self.assertIn("不要承诺", block)

    def test_augment_messages_appends_to_system(self):
        messages = [{"role": "system", "content": "人格"},
                    {"role": "user", "content": "你会唱什么"}]
        out = songs.augment_messages(messages)
        self.assertIs(out, messages)
        self.assertTrue(out[0]["content"].startswith("人格\n\n本机点播："))
        self.assertEqual(out[-1], {"role": "user", "content": "你会唱什么"})

    def test_augment_messages_inserts_system_when_missing(self):
        messages = [{"role": "user", "content": "你会唱什么"}]
        out = songs.augment_messages(messages)
        self.assertEqual([m["role"] for m in out], ["system", "user"])
        self.assertIn("人是猫", out[0]["content"])

    def test_augment_messages_passes_non_list_through(self):
        self.assertIsNone(songs.augment_messages(None))

    def test_turn_messages_carry_the_playlist(self):
        import pipeline
        from pipeline import Timers

        class _Bridge:
            tts = None
            memory_store = None
            memory_writer = None
            file_access = None

            def superseded(self, gen):
                return False

        turn = pipeline.Turn(_Bridge(), 1, "你会唱什么", Timers())
        messages = turn._messages()
        self.assertEqual(messages[-1], {"role": "user", "content": "你会唱什么"})
        self.assertIn("本机点播：", messages[0]["content"])
        self.assertIn("《人是猫》", messages[0]["content"])

    def test_stream_bytes(self):
        import io

        import server

        handler = server.Handler.__new__(server.Handler)
        handler.path = "/turn?session_id=test"
        handler.wfile = io.BytesIO()
        handler.send_response = lambda code: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        song = songs.pick("放一首《人是猫》")
        handler._stream_song(song)

        raw = handler.wfile.getvalue()
        pos = 0
        total = 0
        while True:
            end = raw.index(b"\r\n", pos)
            size = int(raw[pos:end], 16)
            pos = end + 2
            if size == 0:
                break
            total += size
            pos += size + 2
        self.assertEqual(total, os.path.getsize(song["path"]))


if __name__ == "__main__":
    unittest.main()
