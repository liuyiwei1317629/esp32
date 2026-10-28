"""「设备最近露面」的测试：落盘、节流、损坏文件、以及 /api/state 的接线。"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import device_seen
import llm
import server


class FakeClock:
    def __init__(self, now=1700000000.0):
        self.now = now

    def __call__(self):
        return self.now


class DeviceSeenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "device_seen.json")
        self.clock = FakeClock()

    def track(self):
        return device_seen.DeviceSeen(self.path, clock=self.clock)

    def saved(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_empty_means_never_seen(self):
        self.assertIsNone(self.track().status())

    def test_record_reports_ip_and_age(self):
        track = self.track()
        track.record("192.168.0.50")
        self.clock.now += 12
        st = track.status()
        self.assertEqual(st["ip"], "192.168.0.50")
        self.assertEqual(st["age_s"], 12)
        self.assertTrue(st["last_seen"])

    def test_persists_across_restart(self):
        self.track().record("192.168.0.50")
        again = self.track()
        self.assertEqual(again.status()["ip"], "192.168.0.50")

    def test_same_ip_does_not_write_every_turn(self):
        track = self.track()
        track.record("192.168.0.50")
        first = self.saved()["last_seen_ts"]
        self.clock.now += 5
        track.record("192.168.0.50")
        second = self.saved()["last_seen_ts"]
        self.assertEqual(first, second)          # 30 秒内不重复落盘
        self.assertEqual(track.status()["age_s"], 0)  # 但内存里是准的

    def test_ip_change_writes_immediately(self):
        track = self.track()
        track.record("192.168.0.50")
        self.clock.now += 5
        track.record("192.168.0.77")
        saved = self.saved()
        self.assertEqual(saved["ip"], "192.168.0.77")

    def test_broken_file_counts_as_never_seen(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(self.track().status())
        self.track().record("192.168.0.9")
        self.assertEqual(self.track().status()["ip"], "192.168.0.9")

    def test_blank_ip_is_ignored(self):
        track = self.track()
        track.record("   ")
        self.assertIsNone(track.status())


class _Bridge:
    def __init__(self, track):
        self.device_seen = track
        self.file_access = None

    def interrupt(self):
        return 1


class DeviceStateHttpTest(unittest.TestCase):
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
        self.track = device_seen.DeviceSeen(
            os.path.join(self.tmp.name, "device_seen.json"))
        self._saved = (server.Handler.bridge, server.Handler.asr)
        server.Handler.bridge = _Bridge(self.track)
        server.Handler.asr = None
        patches = [patch.object(config, "auth_token", return_value=None),
                   # 兜底路径（bridge 没有 file_access 时）会新建 PermissionStore，
                   # 指向真实 ~/.voice-ai/permissions.json；这里改成临时文件。
                   patch.object(config, "FILE_PERMS_FILE",
                                os.path.join(self.tmp.name, "permissions.json")),
                   patch.object(llm, "brain_status", return_value={"active": "deepseek"}),
                   patch.object(llm, "profile_names", return_value=[]),
                   patch.object(llm, "profile_meta", return_value={})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._restore)

    def _restore(self):
        server.Handler.bridge, server.Handler.asr = self._saved

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def test_state_has_null_device_before_first_visit(self):
        import requests
        state = requests.get(self.url("/api/state"), timeout=5).json()
        self.assertIn("device", state)
        self.assertIsNone(state["device"])

    def test_lan_request_records_ip(self):
        import requests
        fake = type("Addr", (), {"is_loopback": False})()
        with patch.object(server.ipaddress, "ip_address", return_value=fake):
            resp = requests.post(self.url("/stop"), timeout=5)
        self.assertEqual(resp.status_code, 200)
        state = requests.get(self.url("/api/state"), timeout=5).json()
        self.assertEqual(state["device"]["ip"], "127.0.0.1")
        self.assertLess(state["device"]["age_s"], 5)

    def test_loopback_requests_are_not_a_device(self):
        import requests
        requests.post(self.url("/stop"), timeout=5)
        state = requests.get(self.url("/api/state"), timeout=5).json()
        self.assertIsNone(state["device"])

    def test_console_has_device_chip(self):
        import requests
        html = requests.get(self.url("/console"), timeout=5).text
        self.assertIn('id="chipDevice"', html)
        self.assertIn("fmtAgo", html)


if __name__ == "__main__":
    unittest.main()
