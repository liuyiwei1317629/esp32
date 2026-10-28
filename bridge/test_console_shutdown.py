"""调试台「一键关闭」接口的测试。

真的关闭会把测试服务器一起停掉，所以这里把 _shutdown_soon 换成记录器，
只验证：鉴权、只允许本机、打断当前轮、确实发起了关闭。
"""

import os
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import server


class _Bridge:
    def __init__(self):
        self.interrupted = 0

    def interrupt(self):
        self.interrupted += 1
        return 7


class ShutdownTest(unittest.TestCase):
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
        self._saved = (server.Handler.bridge, server.Handler.asr)
        self.bridge = _Bridge()
        server.Handler.bridge = self.bridge
        server.Handler.asr = None
        self.calls = []
        self._patch = patch.object(
            server, "_shutdown_soon", side_effect=lambda httpd: self.calls.append(httpd))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        server.Handler.bridge, server.Handler.asr = self._saved

    def post(self, headers=None):
        import requests
        return requests.post("http://127.0.0.1:%d/shutdown" % self.port,
                             json={}, headers=headers or {}, timeout=5)

    def test_requires_auth(self):
        with patch.object(config, "auth_token", return_value="secret"):
            resp = self.post()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.bridge.interrupted, 0)
        self.assertEqual(self.calls, [])

    def test_rejects_non_loopback(self):
        with patch.object(config, "auth_token", return_value=None):
            fake = type("Addr", (), {"is_loopback": False})()
            with patch.object(server.ipaddress, "ip_address", return_value=fake):
                resp = self.post()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.bridge.interrupted, 0)
        self.assertEqual(self.calls, [])

    def test_loopback_interrupts_and_schedules_stop(self):
        with patch.object(config, "auth_token", return_value=None):
            resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "stopping": True})
        self.assertEqual(self.bridge.interrupted, 1)
        deadline = time.time() + 2
        while not self.calls and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.calls[0], self.httpd)

    def test_console_button_present(self):
        from unittest.mock import patch as p
        with p.object(config, "auth_token", return_value=None):
            import requests
            html = requests.get("http://127.0.0.1:%d/console" % self.port, timeout=5).text
        self.assertIn('id="btnQuit"', html)
        self.assertIn("doQuit", html)
        self.assertIn('api("/shutdown"', html)


if __name__ == "__main__":
    unittest.main()
