"""调试台「一键重启」的测试。

真的重启会把测试服务器停掉、还会开新窗口，所以这里把
_spawn_relauncher 和 _shutdown_soon 换成记录器，只验证：
鉴权、只允许本机、启动器存在、确实拉起了等候进程。
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
import relaunch
import server


class _Bridge:
    def __init__(self):
        self.interrupted = 0

    def interrupt(self):
        self.interrupted += 1
        return 3


class RestartTest(unittest.TestCase):
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
        self.spawns = []
        self.calls = []
        self._p1 = patch.object(server, "_spawn_relauncher",
                                side_effect=lambda: self.spawns.append(True))
        self._p2 = patch.object(server, "_shutdown_soon",
                                side_effect=lambda httpd, *a: self.calls.append(httpd))
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        server.Handler.bridge, server.Handler.asr = self._saved

    def post(self, headers=None):
        import requests
        return requests.post("http://127.0.0.1:%d/restart" % self.port,
                             json={}, headers=headers or {}, timeout=5)

    def test_requires_auth(self):
        with patch.object(config, "auth_token", return_value="secret"):
            resp = self.post()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.bridge.interrupted, 0)

    def test_rejects_non_loopback(self):
        with patch.object(config, "auth_token", return_value=None):
            fake = type("Addr", (), {"is_loopback": False})()
            with patch.object(server.ipaddress, "ip_address", return_value=fake):
                resp = self.post()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.bridge.interrupted, 0)

    def test_missing_launcher_is_refused(self):
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(server, "RESTART_CMD", os.path.join(server.BRIDGE_DIR, "nope.cmd")):
            resp = self.post()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.bridge.interrupted, 0)

    def test_restart_spawns_and_schedules_stop(self):
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(server, "RESTART_CMD", os.path.abspath(__file__)):
            resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "restarting": True})
        self.assertEqual(self.spawns, [True])
        self.assertEqual(self.bridge.interrupted, 1)
        deadline = time.time() + 2
        while not self.calls and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.calls[0], self.httpd)

    def test_console_button_present(self):
        with patch.object(config, "auth_token", return_value=None):
            import requests
            html = requests.get("http://127.0.0.1:%d/console" % self.port, timeout=5).text
        self.assertIn('id="btnRestart"', html)
        self.assertIn("doRestart", html)
        self.assertIn('api("/restart"', html)


class RelaunchHelperTest(unittest.TestCase):
    def test_wait_gone_when_process_and_port_are_clear(self):
        with patch.object(relaunch, "pid_alive", return_value=False), \
                patch.object(relaunch, "port_open", return_value=False):
            self.assertTrue(relaunch.wait_gone(1, 8080, pid_timeout=0.5,
                                               port_timeout=0.5, step=0.01))

    def test_wait_gone_gives_up_when_process_stays(self):
        with patch.object(relaunch, "log"), \
                patch.object(relaunch, "pid_alive", return_value=True):
            self.assertFalse(relaunch.wait_gone(1, 8080, pid_timeout=0.1,
                                                port_timeout=0.1, step=0.01))

    def test_wait_gone_gives_up_when_port_stays(self):
        with patch.object(relaunch, "log"), \
                patch.object(relaunch, "pid_alive", return_value=False), \
                patch.object(relaunch, "port_open", return_value=True):
            self.assertFalse(relaunch.wait_gone(1, 8080, pid_timeout=0.1,
                                                port_timeout=0.1, step=0.01))

    def test_main_starts_the_launcher(self):
        opened = []
        with patch.object(relaunch, "log"), \
                patch.object(relaunch, "wait_gone", return_value=True), \
                patch.object(relaunch.os, "startfile", side_effect=opened.append):
            code = relaunch.main(["--pid", "1", "--port", "8080",
                                  "--cmd", "C:\\somewhere\\start-bridge.cmd",
                                  "--settle", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(opened, ["C:\\somewhere\\start-bridge.cmd"])

    def test_main_skips_launch_when_old_process_hangs(self):
        opened = []
        with patch.object(relaunch, "log"), \
                patch.object(relaunch, "wait_gone", return_value=False), \
                patch.object(relaunch.os, "startfile", side_effect=opened.append):
            code = relaunch.main(["--pid", "1", "--port", "8080",
                                  "--cmd", "C:\\somewhere\\start-bridge.cmd"])
        self.assertEqual(code, 1)
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()
