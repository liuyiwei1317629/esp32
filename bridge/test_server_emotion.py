"""情绪标签接进 /turn 的回归测试：回复文字 → 屏幕动画。"""
import contextlib
import io
import os
import random
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import screen_hub
import server


class FakeTurn:
    gen = 7
    error = None
    stats = {"profile": "deepseek", "usage": None}
    reply_text = ""

    def pcm_chunks(self):
        yield b"\x00\x00" * 160


class FakeBridge:
    def __init__(self, reply):
        self.reply = reply

    def begin(self, text, timers):
        turn = FakeTurn()
        turn.reply_text = self.reply
        return turn


class FakeAsr:
    def transcribe_wav(self, body):
        return "测试原话", 0.01


class RecordingHub(screen_hub.ScreenHub):
    def __init__(self):
        screen_hub.ScreenHub.__init__(
            self, lib=None, clock=time.monotonic, rng=random.Random(1))
        self.calls = []

    def first_audio(self, hint=None, emotion=None):
        screen_hub.ScreenHub.first_audio(self, hint=hint, emotion=emotion)
        self.calls.append((hint, emotion, self.anim))


class ServerEmotionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.old = (server.Handler.bridge, server.Handler.asr,
                    server.Handler.screen_hub)
        server.Handler.asr = FakeAsr()
        self.hub = RecordingHub()
        server.Handler.screen_hub = self.hub
        p = patch.object(server, "report", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        server.Handler.bridge, server.Handler.asr, server.Handler.screen_hub = self.old

    def turn(self, path="/turn", body=b"fake-wav"):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            with contextlib.redirect_stdout(io.StringIO()):
                return requests.post(
                    "http://127.0.0.1:%d%s?session_id=emotest" % (self.port, path),
                    data=body, timeout=10)

    def test_device_turn_plays_emotion_animation(self):
        server.Handler.bridge = FakeBridge("太棒了，我迫不及待想试试")
        resp = self.turn()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.hub.calls), 1)
        _hint, emotion, anim = self.hub.calls[0]
        self.assertEqual(emotion, "excited")
        self.assertEqual(anim, screen_hub.EMOTION_ANIMS["excited"])
        self.assertEqual(self.hub.state, "idle")      # 播完回到待机

    def test_neutral_reply_keeps_playing_animation(self):
        server.Handler.bridge = FakeBridge("现在十点零三分。")
        self.turn()
        self.assertEqual(self.hub.calls[0][1], "calm")
        self.assertEqual(self.hub.calls[0][2], screen_hub.PLAYING)

    def test_console_text_does_not_touch_the_screen(self):
        server.Handler.bridge = FakeBridge("太棒了")
        resp = self.turn(path="/api/text",
                         body='{"text": "太棒了"}'.encode("utf-8"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.hub.calls, [])


if __name__ == "__main__":
    unittest.main()
