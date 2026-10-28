"""人设词接口的测试：只允许本机改，上限沿用记忆链路的 120 字。"""

import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import pipeline
import server


class PersonaHttpTest(unittest.TestCase):
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
        self.file = os.path.join(self.tmp.name, "persona.txt")
        patcher = patch.object(config, "PERSONA_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def url(self):
        return "http://127.0.0.1:%d/api/persona" % self.port

    def test_get_defaults(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            data = requests.get(self.url(), timeout=5).json()
        self.assertEqual(data["text"], config.DEFAULT_PERSONA)
        self.assertFalse(data["custom"])
        self.assertEqual(data["max_chars"], 120)
        self.assertEqual(data["default"], config.DEFAULT_PERSONA)

    def test_save_and_read_back(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            saved = requests.post(self.url(), json={"text": "  你是小鱼，说话短。  "},
                                  timeout=5)
            self.assertEqual(saved.status_code, 200, saved.text)
            data = saved.json()
            self.assertEqual(data["text"], "你是小鱼，说话短。")
            self.assertTrue(data["custom"])
            again = requests.get(self.url(), timeout=5).json()
        self.assertEqual(again["text"], "你是小鱼，说话短。")
        with open(self.file, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "你是小鱼，说话短。\n")

    def test_empty_restores_default(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            requests.post(self.url(), json={"text": "临时人设"}, timeout=5)
            data = requests.post(self.url(), json={"text": "   "}, timeout=5).json()
        self.assertEqual(data["text"], config.DEFAULT_PERSONA)
        self.assertFalse(data["custom"])
        self.assertFalse(os.path.exists(self.file))

    def test_over_limit_rejected(self):
        import requests
        too_long = "啊" * 121
        with patch.object(config, "auth_token", return_value=None):
            resp = requests.post(self.url(), json={"text": too_long}, timeout=5)
            after = requests.get(self.url(), timeout=5).json()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("121", resp.json()["error"])
        self.assertIn("120", resp.json()["error"])
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual(after["text"], config.DEFAULT_PERSONA)

    def test_exactly_limit_is_ok(self):
        import requests
        text = "鱼" * 120
        with patch.object(config, "auth_token", return_value=None):
            resp = requests.post(self.url(), json={"text": text}, timeout=5)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["text"], text)

    def test_rejects_bad_json_and_type(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            bad = requests.post(self.url(), data=b"{oops",
                                headers={"Content-Type": "application/json"}, timeout=5)
            wrong = requests.post(self.url(), json={"text": ["a", "b"]}, timeout=5)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(wrong.status_code, 400)
        self.assertFalse(os.path.exists(self.file))

    def test_rejects_non_loopback(self):
        import requests
        fake = type("Addr", (), {"is_loopback": False})()
        with patch.object(config, "auth_token", return_value=None):
            with patch.object(server.ipaddress, "ip_address", return_value=fake):
                listing = requests.get(self.url(), timeout=5)
                saving = requests.post(self.url(), json={"text": "别写"}, timeout=5)
        self.assertEqual(listing.status_code, 403)
        self.assertEqual(saving.status_code, 403)
        self.assertFalse(os.path.exists(self.file))

    def test_requires_auth(self):
        import requests
        with patch.object(config, "auth_token", return_value="secret"):
            listing = requests.get(self.url(), timeout=5)
            saving = requests.post(self.url(), json={"text": "x"}, timeout=5)
        self.assertEqual(listing.status_code, 401)
        self.assertEqual(saving.status_code, 401)

    def test_turn_uses_saved_persona(self):
        config.save_persona("你是测试鱼，只说短句。")
        bridge = type("B", (), {"memory_store": None, "file_access": None})()
        with patch.object(config, "MEMORY_ENABLED", False):
            turn = pipeline.Turn(bridge, 1, "你好", pipeline.Timers(), session_id="web")
            messages = turn._messages()
        self.assertEqual(messages[0]["role"], "system")
        # 歌单提示块会接在人设后面，所以只断言开头。
        self.assertTrue(messages[0]["content"].startswith("你是测试鱼，只说短句。"))

    def test_console_panel_present_and_spells_out_limits(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            html = requests.get("http://127.0.0.1:%d/console" % self.port,
                                timeout=5).text
        self.assertIn('id="btnPersona"', html)
        self.assertIn("showPersona", html)
        self.assertIn('api("/api/persona"', html)
        self.assertIn("人设词的要求", html)
        for word in ("120 字", "1050 token", "下一轮生效", "恢复默认", "本地 2B"):
            self.assertIn(word, html, word)
        self.assertIn("服务端还是旧代码", html)


if __name__ == "__main__":
    unittest.main()
