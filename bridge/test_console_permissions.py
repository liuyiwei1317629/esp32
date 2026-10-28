"""调试台「权限」接口的测试。

权限决定 AI 能读哪些文件，所以只允许本机改；这里用临时目录和临时
permissions.json，不碰真实的 ~/.voice-ai/permissions.json。
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
import file_access
import server


class _Bridge:
    def __init__(self, access):
        self.file_access = access

    def interrupt(self):
        return 1


class PermissionsHttpTest(unittest.TestCase):
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
        self.root = self.tmp.name
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(os.path.join(self.vault, "sub"))
        self.store = file_access.PermissionStore(
            os.path.join(self.root, "permissions.json"))
        self._saved = (server.Handler.bridge, server.Handler.asr)
        server.Handler.bridge = _Bridge(file_access.FileAccess(store=self.store))
        server.Handler.asr = None
        # 双保险：万一走到 _perms_store 的兜底分支，也不能碰真实权限文件
        self._perms_patch = patch.object(
            config, "FILE_PERMS_FILE", os.path.join(self.root, "real-perms.json"))
        self._perms_patch.start()
        self.addCleanup(self._perms_patch.stop)
        self.addCleanup(self._restore)

    def _restore(self):
        server.Handler.bridge, server.Handler.asr = self._saved

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def test_requires_auth(self):
        import requests
        with patch.object(config, "auth_token", return_value="secret"):
            resp = requests.get(self.url("/api/permissions"), timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_list_starts_empty(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            data = requests.get(self.url("/api/permissions"), timeout=5).json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["folders"], [])
        self.assertEqual(data["active"], 0)
        self.assertEqual(data["perms_file"], self.store.path)

    def test_add_toggle_remove_roundtrip(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            bad = requests.post(self.url("/api/permissions/add"),
                                json={"path": self.vault}, timeout=5)
            self.assertEqual(bad.status_code, 400)

            added = requests.post(self.url("/api/permissions/add"),
                                  json={"path": self.vault, "confirm": True},
                                  timeout=5)
            self.assertEqual(added.status_code, 200)
            item = added.json()["item"]
            self.assertTrue(item["enabled"])
            self.assertEqual(added.json()["permissions"]["active"], 1)
            self.assertTrue(os.path.exists(self.store.path))

            off = requests.post(self.url("/api/permissions/toggle"),
                                json={"id": item["id"], "enabled": False}, timeout=5)
            self.assertEqual(off.status_code, 200, off.text)
            self.assertEqual(off.json()["permissions"]["active"], 0)

            gone = requests.post(self.url("/api/permissions/remove"),
                                 json={"id": item["id"]}, timeout=5)
            self.assertEqual(gone.status_code, 200)
            self.assertEqual(gone.json()["permissions"]["folders"], [])

            again = requests.post(self.url("/api/permissions/remove"),
                                  json={"id": item["id"]}, timeout=5)
            self.assertEqual(again.status_code, 404)

    def test_dirs_lists_subfolders(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            data = requests.get(self.url("/api/permissions/dirs"),
                                params={"path": self.vault}, timeout=5).json()
        self.assertEqual(data["path"], self.vault)
        self.assertEqual([d["name"] for d in data["dirs"]], ["sub"])
        self.assertTrue(data["parent"])

    def test_rejects_non_loopback(self):
        import requests
        fake = type("Addr", (), {"is_loopback": False})()
        with patch.object(config, "auth_token", return_value=None):
            with patch.object(server.ipaddress, "ip_address", return_value=fake):
                listing = requests.get(self.url("/api/permissions"), timeout=5)
                adding = requests.post(self.url("/api/permissions/add"),
                                       json={"path": self.vault, "confirm": True},
                                       timeout=5)
        self.assertEqual(listing.status_code, 403)
        self.assertEqual(adding.status_code, 403)
        self.assertEqual(self.store.items(), [])

    def test_console_panel_present(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            html = requests.get(self.url("/console"), timeout=5).text
        self.assertIn('id="btnPerm"', html)
        self.assertIn('id="chipFiles"', html)
        self.assertIn("showPermissions", html)
        self.assertIn('api("/api/permissions"', html)


if __name__ == "__main__":
    unittest.main()
