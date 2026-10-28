"""调试台记忆编辑接口的测试。

只碰临时目录：这些端点会写个人数据，测试绝不能用真实记忆库。
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import memory_store
import server


class _Bridge:
    def __init__(self, store):
        self.memory_store = store


class MemoryEditTest(unittest.TestCase):
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
        self.tmp = tempfile.mkdtemp(prefix="mem-edit-")
        self.store = memory_store.MemoryStore(root=self.tmp)
        self._saved = (server.Handler.bridge, server.Handler.asr)
        server.Handler.bridge = _Bridge(self.store)
        server.Handler.asr = None

    def tearDown(self):
        server.Handler.bridge, server.Handler.asr = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            return requests.get(self.url(path), timeout=5)

    def post(self, path, payload):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            return requests.post(self.url(path), json=payload, timeout=5)

    def items(self):
        resp = self.get("/api/memory?text=1")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    # ---- 读 -------------------------------------------------------------
    def test_requires_token(self):
        import requests
        with patch.object(config, "auth_token", return_value="secret"):
            resp = requests.get(self.url("/api/memory?text=1"), timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_counts_do_not_leak_text_by_default(self):
        self.store.upsert_fact("fact", "开饭时间是 9 点到 17 点")
        data = self.get("/api/memory").json()
        self.assertEqual(data["facts"], 1)
        self.assertNotIn("fact_items", data)

    def test_text_and_filters(self):
        self.store.upsert_fact("fact", "开饭时间是 9 点到 17 点")
        self.store.upsert_fact("preference", "喜欢安静")
        self.store.upsert_episode("todo", "明天去买电池")
        data = self.items()
        self.assertEqual(len(data["fact_items"]), 2)
        self.assertEqual(len(data["episode_items"]), 1)

        only_pref = self.get("/api/memory?text=1&kind=preference").json()
        self.assertEqual([i["text"] for i in only_pref["fact_items"]], ["喜欢安静"])

        searched = self.get("/api/memory?text=1&q=电池").json()
        self.assertEqual(searched["fact_items"], [])
        self.assertEqual(len(searched["episode_items"]), 1)

    # ---- 写 -------------------------------------------------------------
    def test_add_fact_and_pin(self):
        resp = self.post("/api/memory/fact",
                         {"kind": "fact", "text": "开饭时间是 9 点到 17 点"})
        self.assertEqual(resp.status_code, 200)
        item = resp.json()["item"]
        self.assertFalse(item["pinned"])

        resp = self.post("/api/memory/pin", {"id": item["id"], "pinned": True})
        self.assertEqual(resp.status_code, 200)
        fact = self.store.find_fact(item["id"])
        self.assertTrue(fact["pinned"])

    def test_add_empty_text_is_400(self):
        resp = self.post("/api/memory/fact", {"kind": "fact", "text": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_add_with_pinned_flag(self):
        resp = self.post("/api/memory/fact",
                         {"kind": "name", "text": "叫我老张", "pinned": True})
        self.assertTrue(resp.json()["item"]["pinned"])

    def test_edit_archives_the_old_record(self):
        first = self.post("/api/memory/fact",
                          {"kind": "fact", "text": "开饭时间是 9 点"}).json()["item"]
        resp = self.post("/api/memory/fact", {
            "kind": "fact", "text": "开饭时间是 9 点到 17 点",
            "replace_id": first["id"],
        })
        self.assertEqual(resp.status_code, 200)
        new = resp.json()["item"]
        self.assertNotEqual(new["id"], first["id"])

        self.assertEqual(len(self.store.active_facts()), 1)
        old = self.store.find_fact(first["id"])
        self.assertEqual(old["status"], "tombstone")
        self.assertEqual(old["tombstone_reason"], "superseded")

    def test_edit_unknown_id_is_404(self):
        resp = self.post("/api/memory/fact",
                         {"kind": "fact", "text": "x", "replace_id": "nope"})
        self.assertEqual(resp.status_code, 404)

    def test_delete_tombstones_but_keeps_record(self):
        item = self.post("/api/memory/fact",
                         {"kind": "fact", "text": "临时记一下"}).json()["item"]
        resp = self.post("/api/memory/delete", {"id": item["id"]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(self.store.active_facts(), [])
        self.assertEqual(self.store.find_fact(item["id"])["status"], "tombstone")

    def test_delete_episode(self):
        episode = self.store.upsert_episode("todo", "明天去买电池")
        resp = self.post("/api/memory/delete", {"id": episode["id"]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.store.active_episodes(), [])

    def test_delete_unknown_is_404(self):
        self.assertEqual(self.post("/api/memory/delete", {"id": "nope"}).status_code, 404)

    def test_bad_json_is_400(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            resp = requests.post(self.url("/api/memory/fact"), data=b"{oops",
                                 timeout=5)
        self.assertEqual(resp.status_code, 400)

    def test_disabled_memory_is_409(self):
        server.Handler.bridge = _Bridge(None)
        resp = self.post("/api/memory/fact", {"kind": "fact", "text": "x"})
        self.assertEqual(resp.status_code, 409)

    def test_read_only_store_is_409(self):
        self.store.read_only = True
        resp = self.post("/api/memory/fact", {"kind": "fact", "text": "x"})
        self.assertEqual(resp.status_code, 409)

    def test_console_page_ships_the_editor(self):
        resp = self.get("/console")
        self.assertIn("/api/memory/fact", resp.text)
        self.assertIn("/api/memory/pin", resp.text)
        self.assertIn("/api/memory/delete", resp.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
