"""放音音量的测试：档位持久化、非法值拒绝、PCM 衰减。

用 PLAYBACK_FILE 指向临时文件，不碰真实的 ~/.voice-ai/playback.json。
"""

import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import playback


class PlaybackLevelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "nested", "playback.json")
        self._old = os.environ.get("PLAYBACK_FILE")
        os.environ["PLAYBACK_FILE"] = self.path
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("PLAYBACK_FILE", None)
        else:
            os.environ["PLAYBACK_FILE"] = self._old

    def test_defaults_to_full(self):
        self.assertEqual(playback.level(), 100)
        self.assertEqual(playback.gain(), 1.0)

    def test_set_and_persist(self):
        self.assertEqual(playback.set_level(50), 50)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"level": 50})
        self.assertEqual(playback.level(), 50)
        self.assertEqual(playback.gain(), 0.5)

    def test_set_accepts_numeric_string(self):
        self.assertEqual(playback.set_level("70"), 70)
        self.assertEqual(playback.level(), 70)

    def test_rejects_unknown_levels(self):
        for bad in (0, 33, 101, -30, "abc", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                playback.set_level(bad)
        self.assertFalse(os.path.exists(self.path))

    def test_corrupt_file_falls_back_to_full(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(playback.level(), 100)

    def test_out_of_set_stored_value_falls_back_to_full(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"level": 33}, fh)
        self.assertEqual(playback.level(), 100)


class PlaybackApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = os.environ.get("PLAYBACK_FILE")
        os.environ["PLAYBACK_FILE"] = os.path.join(self.tmp.name, "playback.json")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("PLAYBACK_FILE", None)
        else:
            os.environ["PLAYBACK_FILE"] = self._old

    def test_full_level_returns_input_untouched(self):
        pcm = b"\x01\x02\x03"
        self.assertIs(playback.apply(pcm), pcm)
        self.assertEqual(playback.apply(b""), b"")

    def test_half_level_scales_samples_and_keeps_tail_byte(self):
        playback.set_level(50)
        pcm = np.array([1000, -2000, 32767, -32768], dtype="<i2").tobytes() + b"\x7f"
        out = playback.apply(pcm)
        self.assertEqual(len(out), len(pcm))
        self.assertEqual(out[-1:], b"\x7f")
        got = np.frombuffer(out[:-1], dtype="<i2")
        np.testing.assert_allclose(got, [500, -1000, 16383, -16384], atol=1)

    def test_low_level_never_clips_or_grows(self):
        playback.set_level(30)
        samples = np.array([32767, -32768, 12345, -12345] * 100, dtype="<i2")
        out = np.frombuffer(playback.apply(samples.tobytes()), dtype="<i2")
        self.assertLessEqual(np.max(np.abs(out.astype(np.int32))), 32767)
        np.testing.assert_allclose(out, samples * 0.3, atol=2)


if __name__ == "__main__":
    unittest.main()
