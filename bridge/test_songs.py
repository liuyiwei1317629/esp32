import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import songs


def wav_bytes(seconds=1.0, rate=44100, channels=2):
    import numpy as np
    import soundfile as sf
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    wave = (0.2 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    data = np.stack([wave] * channels, axis=1)
    buf = io.BytesIO()
    sf.write(buf, data, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "songs")
        os.makedirs(self.dir)
        self.old_dir, self.old_playlist = songs.SONGS_DIR, songs.PLAYLIST
        songs.SONGS_DIR = songs.Path(self.dir)
        songs.PLAYLIST = songs.SONGS_DIR / "playlist.json"
        songs.reset()
        self.addCleanup(self._restore)

    def _restore(self):
        songs.SONGS_DIR, songs.PLAYLIST = self.old_dir, self.old_playlist
        songs.reset()

    def playlist(self):
        with open(songs.PLAYLIST, encoding="utf-8") as fh:
            return json.load(fh)["songs"]

    def test_upload_converts_to_16k_mono_pcm(self):
        entry = songs.add_song("测试歌", "upload.wav", wav_bytes(1.0, 44100, 2))
        self.assertEqual(entry["title"], "测试歌")
        self.assertEqual(entry["file"], "upload.pcm")
        path = os.path.join(self.dir, entry["file"])
        size = os.path.getsize(path)
        self.assertAlmostEqual(size / 32000.0, 1.0, delta=0.05)
        self.assertAlmostEqual(entry["seconds"], size / 32000.0, places=3)

    def test_upload_shows_up_in_playlist_and_pick(self):
        songs.add_song("测试歌", "upload.wav", wav_bytes(0.5))
        self.assertEqual([s["title"] for s in self.playlist()], ["测试歌"])
        self.assertEqual(songs.pick("放一首《测试歌》")["title"], "测试歌")
        self.assertIn("《测试歌》", songs.prompt_block())

    def test_upload_keeps_existing_songs(self):
        songs.add_song("一号", "a.wav", wav_bytes(0.3))
        songs.add_song("二号", "b.wav", wav_bytes(0.3))
        self.assertEqual([s["title"] for s in self.playlist()], ["一号", "二号"])

    def test_empty_title_falls_back_to_filename(self):
        entry = songs.add_song("  ", "my-song.mp3", wav_bytes(0.3))
        self.assertEqual(entry["title"], "my-song")

    def test_same_filename_twice_does_not_overwrite(self):
        one = songs.add_song("一", "same.wav", wav_bytes(0.3))
        two = songs.add_song("二", "same.wav", wav_bytes(0.3))
        self.assertNotEqual(one["file"], two["file"])
        for name in (one["file"], two["file"]):
            self.assertTrue(os.path.isfile(os.path.join(self.dir, name)))

    def test_rejects_unknown_extension(self):
        with self.assertRaises(ValueError):
            songs.add_song("坏", "notes.txt", b"hello")
        self.assertEqual(os.listdir(self.dir), [])

    def test_rejects_broken_audio(self):
        with self.assertRaises(ValueError):
            songs.add_song("坏", "broken.mp3", b"not really mp3" * 10)
        self.assertEqual(os.listdir(self.dir), [])

    def test_rejects_too_large(self):
        with self.assertRaises(ValueError):
            songs.add_song("大", "big.mp3", b"\x00" * (songs.MAX_UPLOAD_BYTES + 1))
        self.assertEqual(os.listdir(self.dir), [])

    def test_rejects_too_long(self):
        with self.assertRaises(ValueError):
            songs.add_song("长", "long.wav", wav_bytes(songs.MAX_SECONDS + 1, 8000, 1))
        self.assertEqual(os.listdir(self.dir), [])

    def test_remove_deletes_file_and_entry(self):
        a = songs.add_song("一", "a.wav", wav_bytes(0.3))
        songs.add_song("二", "b.wav", wav_bytes(0.3))
        songs.remove_song(a["file"])
        self.assertEqual([s["title"] for s in self.playlist()], ["二"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, a["file"])))

    def test_remove_unknown_song_raises(self):
        with self.assertRaises(KeyError):
            songs.remove_song("nope.pcm")

    def test_remove_does_not_escape_songs_dir(self):
        outside = os.path.join(self.tmp.name, "keep.pcm")
        with open(outside, "wb") as fh:
            fh.write(b"\x00\x00")
        with self.assertRaises(KeyError):
            songs.remove_song("../keep.pcm")
        self.assertTrue(os.path.isfile(outside))

    def test_catalog_flags_missing_files(self):
        entry = songs.add_song("一", "a.wav", wav_bytes(0.3))
        os.remove(os.path.join(self.dir, entry["file"]))
        rows = songs.catalog()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["missing"])


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
