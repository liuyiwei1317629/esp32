"""调试台「放音音量」接口与页面控件的测试。

和权限接口一样只允许本机；档位写临时 PLAYBACK_FILE，不碰真实配置。
"""

import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import playback
import server


class _Bridge:
    def __init__(self):
        self.tts = None

    def interrupt(self):
        return 1


class VolumeHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = os.environ.get("PLAYBACK_FILE")
        os.environ["PLAYBACK_FILE"] = os.path.join(self.tmp.name, "playback.json")
        self.addCleanup(self._restore_env)
        self._saved = (server.Handler.bridge, server.Handler.asr)
        server.Handler.bridge = _Bridge()
        server.Handler.asr = None
        self.addCleanup(self._restore)

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("PLAYBACK_FILE", None)
        else:
            os.environ["PLAYBACK_FILE"] = self._old

    def _restore(self):
        server.Handler.bridge, server.Handler.asr = self._saved

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def post(self, payload, headers=None):
        import requests
        return requests.post(self.url("/api/volume"), json=payload,
                             headers=headers or {}, timeout=5)

    def state(self):
        import requests
        return requests.get(self.url("/api/state"), timeout=5).json()

    def test_requires_auth(self):
        with patch.object(config, "auth_token", return_value="secret"):
            resp = self.post({"level": 50})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(playback.level(), 100)

    def test_rejects_non_loopback(self):
        with patch.object(config, "auth_token", return_value=None):
            fake = type("Addr", (), {"is_loopback": False})()
            with patch.object(server.ipaddress, "ip_address", return_value=fake):
                resp = self.post({"level": 50})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(playback.level(), 100)

    def test_rejects_bad_level(self):
        with patch.object(config, "auth_token", return_value=None):
            resp = self.post({"level": 33})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(playback.level(), 100)

    def test_sets_level_and_reports_it(self):
        with patch.object(config, "auth_token", return_value=None):
            resp = self.post({"level": 50})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"ok": True, "level": 50})
            self.assertEqual(playback.level(), 50)
            volume = self.state()["volume"]
        self.assertEqual(volume, {"level": 50, "levels": [30, 50, 70, 100]})

    def test_console_has_volume_control(self):
        import requests
        html = requests.get(self.url("/console"), timeout=5).text
        self.assertIn('id="volumeSel"', html)
        self.assertIn('$("#volumeSel").onchange = setVolume;', html)
        self.assertIn('api("/api/volume"', html)
        self.assertIn("音量 小", html)
        self.assertIn("音量 满", html)


if __name__ == "__main__":
    unittest.main()
