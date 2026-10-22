import contextlib
import io
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
import llm
import server
import usage


class FakeTurn:
    gen = 3
    error = None
    stats = {
        "profile": "deepseek",
        "fallback_used": True,
        "attempts": 2,
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
        "content_chars": 4,
    }

    def pcm_chunks(self):
        yield b"\x00\x00" * 160


class FakeBridge:
    def begin(self, text, timers):
        return FakeTurn()


class FakeAsr:
    def transcribe_wav(self, body):
        return "不要写进账本的用户原话", 0.01


class ServerLedgerTest(unittest.TestCase):
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
        self.path = os.path.join(self.tmp.name, "usage.jsonl")
        self.old_bridge = server.Handler.bridge
        self.old_asr = server.Handler.asr
        server.Handler.bridge = FakeBridge()
        server.Handler.asr = FakeAsr()
        report_patch = patch.object(server, "report", return_value=None)
        report_patch.start()
        self.addCleanup(report_patch.stop)

    def tearDown(self):
        server.Handler.bridge = self.old_bridge
        server.Handler.asr = self.old_asr
        self.tmp.cleanup()

    def turn(self):
        import requests
        url = "http://127.0.0.1:%d/turn?session_id=ledgertest" % self.port
        return requests.post(url, data=b"fake-wav", timeout=10)

    def wait_for_ledger(self):
        for _ in range(100):
            if os.path.isfile(self.path):
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
                if raw.strip():
                    return raw
            time.sleep(0.01)
        self.fail("usage ledger was not written")

    def test_turn_writes_ledger_without_user_text(self):
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(config, "USAGE_LEDGER_FILE", self.path), \
                patch.object(llm, "profile_prices", return_value=(2.0, 8.0)):
            with contextlib.redirect_stdout(io.StringIO()):
                resp = self.turn()
        self.assertEqual(resp.status_code, 200)
        raw = self.wait_for_ledger()
        entry = json.loads(raw)
        self.assertEqual(entry["profile"], "deepseek")
        self.assertEqual(entry["prompt_tokens"], 1000)
        self.assertEqual(entry["completion_tokens"], 50)
        self.assertEqual(entry["cost_cny"], 0.0024)
        self.assertTrue(entry["fallback_used"])
        self.assertNotIn("不要写进账本的用户原话", raw)

    def test_ledger_failure_does_not_break_the_audio_response(self):
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(config, "USAGE_LEDGER_FILE", self.path), \
                patch.object(llm, "profile_prices", return_value=(2.0, 8.0)), \
                patch.object(usage, "append_entry", side_effect=OSError("disk full")):
            with contextlib.redirect_stdout(io.StringIO()):
                resp = self.turn()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"\x00\x00" * 160)


if __name__ == "__main__":
    unittest.main(verbosity=2)
