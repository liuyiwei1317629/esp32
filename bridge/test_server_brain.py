import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import llm
import server


class LanAddressTest(unittest.TestCase):
    def test_loopback_and_private_are_lan(self):
        for host in ("127.0.0.1", "192.168.0.100", "10.1.2.3", "::1", "fd00::1"):
            self.assertTrue(server.is_lan_address(host), host)

    def test_public_and_invalid_are_not_lan(self):
        for host in ("8.8.8.8", "2001:4860:4860::8888", "not-an-ip", ""):
            self.assertFalse(server.is_lan_address(host), host)


class BrainRouteTest(unittest.TestCase):
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

    def post(self, body, headers=None):
        import requests
        url = "http://127.0.0.1:%d/brain" % self.port
        return requests.post(url, json=body, headers=headers or {}, timeout=5)

    def test_switches_profile(self):
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(llm, "set_profile", return_value="kimi"), \
                patch.object(llm, "brain_status", return_value={"active": "kimi",
                                                                "pinned": True,
                                                                "cooling": {}}):
            resp = self.post({"profile": "kimi"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["active"], "kimi")
        self.assertTrue(payload["pinned"])

    def test_unknown_profile_is_400(self):
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(llm, "set_profile", side_effect=ValueError("unknown brain profile: nope")):
            resp = self.post({"profile": "nope"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("unknown", resp.json()["error"])

    def test_missing_profile_is_400(self):
        with patch.object(config, "auth_token", return_value=None):
            resp = self.post({})
        self.assertEqual(resp.status_code, 400)

    def test_requires_auth(self):
        with patch.object(config, "auth_token", return_value="secret"):
            resp = self.post({"profile": "kimi"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers.get("Connection"), "close")

    def test_auth_header_is_accepted(self):
        with patch.object(config, "auth_token", return_value="secret"), \
                patch.object(llm, "set_profile", return_value="kimi"), \
                patch.object(llm, "brain_status", return_value={"active": "kimi",
                                                                "pinned": True,
                                                                "cooling": {}}):
            resp = self.post({"profile": "kimi"}, headers={"X-Auth": "secret"})
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
