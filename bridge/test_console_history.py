"""调试台「对话」接口：读落盘的 turns.jsonl，重播只走 TTS。"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import server


class _Hub:
    """`_history_payload` 只用得到 path。"""

    def __init__(self, path):
        self.path = path


class _Tts:
    def __init__(self):
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        return b"\x00\x01" * 1600, {"engine": "stub", "tts_ms": 12,
                                    "samples": 1600, "fallback": False}


class _Bridge:
    def __init__(self, tts):
        self.tts = tts


class HistoryHttpTest(unittest.TestCase):
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "turns.jsonl")
        rows = [
            {"seq": 1, "at": "2026-10-30T08:00:0%d" % i, "source": "text",
             "user": "你%d" % i, "reply": "答%d" % i, "engine": "feiyu"}
            for i in range(1, 4)
        ]
        with open(self.path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        for patcher in (
                patch.object(server.console_hub, "hub", lambda: _Hub(self.path)),
                patch.object(server.Handler, "bridge", _Bridge(_Tts()))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def test_tail_lines_drops_partial_first_line(self):
        # 从文件中间切开时，第一行可能是半截，必须丢掉
        size = os.path.getsize(self.path)
        lines = server.tail_lines(self.path, 10, max_bytes=size // 2)
        self.assertTrue(lines)
        for ln in lines:
            json.loads(ln.decode("utf-8"))   # 半截行会在这里抛 ValueError
        self.assertEqual(len(server.tail_lines(self.path, 10)), 3)

    def test_history_returns_newest_last(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            data = requests.get(self.url("/api/history?limit=2"), timeout=5).json()
        self.assertEqual(data["count"], 2)
        self.assertEqual([it["user"] for it in data["items"]], ["你2", "你3"])
        self.assertEqual(data["items"][-1]["reply"], "答3")
        self.assertEqual(data["path"], self.path)

    def test_history_missing_file_is_empty(self):
        import requests
        with patch.object(server.console_hub, "hub", lambda: _Hub("")), \
                patch.object(config, "auth_token", return_value=None):
            data = requests.get(self.url("/api/history"), timeout=5).json()
        self.assertEqual(data["count"], 0)

    def test_history_rejects_non_loopback(self):
        import requests
        fake = type("Addr", (), {"is_loopback": False})()
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(server.ipaddress, "ip_address", return_value=fake):
            resp = requests.get(self.url("/api/history"), timeout=5)
        self.assertEqual(resp.status_code, 403)

    def test_replay_streams_pcm(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            resp = requests.post(self.url("/api/replay"), json={"text": "你好呀"},
                                 timeout=10)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers.get("X-Engine"), "stub")
        self.assertEqual(resp.headers.get("X-Voice"),
                         "s16le/%d/mono" % config.SAMPLE_RATE)
        self.assertEqual(len(resp.content), 3200)

    def test_replay_rejects_empty_and_long(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            empty = requests.post(self.url("/api/replay"), json={"text": "  "},
                                  timeout=5)
            long = requests.post(self.url("/api/replay"), json={"text": "长" * 501},
                                 timeout=5)
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(long.status_code, 400)

    def test_replay_rejects_non_loopback(self):
        import requests
        fake = type("Addr", (), {"is_loopback": False})()
        tts = server.Handler.bridge.tts
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(server.ipaddress, "ip_address", return_value=fake):
            resp = requests.post(self.url("/api/replay"), json={"text": "你好"},
                                 timeout=5)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(tts.calls, [])

    def test_console_has_history_panel(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            html = requests.get(self.url("/console"), timeout=5).text
        self.assertIn('id="btnHistory"', html)
        self.assertIn('id="btnBig"', html)
        self.assertIn('api("/api/history', html)
        self.assertIn('api("/api/replay"', html)
        self.assertIn("服务端还是旧代码", html)


if __name__ == "__main__":
    unittest.main()
