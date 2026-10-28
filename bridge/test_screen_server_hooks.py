"""屏幕路由接进 server.py 的回归测试（不起 ASR/TTS）。"""
import importlib
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TOKEN = "unit-test-token"


def read_frames(resp, want):
    buf, frames = b"", []
    for chunk in resp.iter_content(4096):
        buf += chunk
        while True:
            if b"\r\n\r\n" not in buf:
                break
            head, rest = buf.split(b"\r\n\r\n", 1)
            n = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    n = int(line.split(b":")[1])
            if len(rest) < n + 2:
                break
            frames.append(rest[:n])
            buf = rest[n + 2:]
        if len(frames) >= want:
            break
    return frames


class ScreenHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = {k: os.environ.get(k) for k in ("SCREEN_ENABLED", "BRIDGE_TOKEN")}
        os.environ["BRIDGE_TOKEN"] = TOKEN
        os.environ["SCREEN_ENABLED"] = "1"
        import screen_config
        import server
        importlib.reload(screen_config)
        cls.sc = screen_config
        cls.server = importlib.reload(server)
        cls.hub, cls.streamer = cls.server.build_screen()
        cls.server.Handler.screen_hub = cls.hub
        cls.server.Handler.screen_streamer = cls.streamer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.streamer.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def test_disabled_builds_nothing(self):
        saved = self.sc.SCREEN_ENABLED
        self.sc.SCREEN_ENABLED = False
        try:
            self.assertEqual(self.server.build_screen(), (None, None))
        finally:
            self.sc.SCREEN_ENABLED = saved

    def test_stream_serves_jpeg_frames(self):
        r = requests.get(self.url("/screen"), headers={"X-Auth": TOKEN},
                         stream=True, timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertIn("multipart/x-mixed-replace", r.headers["Content-Type"])
        frames = read_frames(r, 2)
        r.close()
        self.assertGreaterEqual(len(frames), 2)
        self.assertEqual(frames[0][:3], b"\xff\xd8\xff")

    def test_stream_needs_auth(self):
        r = requests.get(self.url("/screen"), headers={"X-Auth": "nope"},
                         stream=True, timeout=5)
        self.assertEqual(r.status_code, 401)
        r.close()

    def test_key_route_reports_recording(self):
        r = requests.post(self.url("/screen/key"), headers={"X-Auth": TOKEN}, timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "recording")

    def test_key_route_503_when_disabled(self):
        saved = self.server.Handler.screen_hub
        self.server.Handler.screen_hub = None
        try:
            r = requests.post(self.url("/screen/key"), headers={"X-Auth": TOKEN},
                              timeout=5)
            self.assertEqual(r.status_code, 503)
        finally:
            self.server.Handler.screen_hub = saved


if __name__ == "__main__":
    unittest.main()
