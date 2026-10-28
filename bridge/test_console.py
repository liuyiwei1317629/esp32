import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import console_hub
import llm
import server
import usage


# --------------------------------------------------------------------------
# 假对象：只在测试里用，形状对齐真 Turn / Bridge / Tts
# --------------------------------------------------------------------------
class _FakeAbort:
    def __init__(self):
        self._flag = False

    def is_set(self):
        return self._flag

    def set(self):
        self._flag = True


class _FakeTurn:
    def __init__(self, gen, text, timers, session_id="anon"):
        self.gen = gen
        self.user_text = text
        self.session_id = session_id
        self.timers = timers
        self.stats = {"engine": "cosyvoice", "first_tts_ms": 610, "chunks": 1,
                      "content_chars": 6, "total_seconds": 0.9,
                      "profile": "deepseek", "fallback_used": False,
                      "first_token_seconds": 0.42,
                      "usage": {"prompt_tokens": 10, "completion_tokens": 6,
                                "total_tokens": 16}}
        self.error = None
        self.reply_text = "你好呀。"
        self.completed = False
        self.abort = _FakeAbort()
        self.local_command = ""
        self.local_plugin = False
        self.local_plugin_failed = False
        self.memory_info = {"l1": 2, "l2_chars": 30, "l3": 1, "l4_turns": 2,
                            "total_tokens": 200, "budget_tokens": 1050,
                            "trimmed": [], "warnings": [], "fallback": False,
                            "selected_ids": ["fact:abc"]}

    def pcm_chunks(self):
        yield b"\x00" * 4096
        yield b"\x00" * 2048
        self.completed = True


class _FakeEdge:
    ready = True
    dead_reason = None


class _FakeTts:
    edge = _FakeEdge()
    cosy = None


class _FakeBridge:
    def __init__(self):
        self.tts = _FakeTts()
        self.memory_store = None
        self.memory_writer = None
        self.gen = 0

    def begin(self, text, timers=None, session_id="anon"):
        self.gen += 1
        return _FakeTurn(self.gen, text, timers, session_id)

    def interrupt(self):
        self.gen += 1
        return self.gen


class _HubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="console-")
        console_hub.reset(directory=self.tmp, record=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        console_hub.reset(directory=tempfile.gettempdir(), record=False)


# --------------------------------------------------------------------------
class HubTest(_HubCase):
    def test_seq_is_monotonic_and_since_returns_delta(self):
        hub = console_hub.hub()
        a = hub.record({"user": "one"})
        b = hub.record({"user": "two"})
        self.assertEqual(a["seq"], 1)
        self.assertEqual(b["seq"], 2)

        first = hub.since(0, 0)
        self.assertEqual([t["user"] for t in first["turns"]], ["one", "two"])
        self.assertEqual(first["cursor"]["turn_seq"], 2)

        second = hub.since(2, 0)
        self.assertEqual(second["turns"], [])

    def test_records_are_appended_to_jsonl(self):
        hub = console_hub.hub()
        hub.record({"user": "落盘", "first_audio_ms": 1234})
        hub.record({"user": "第二行"})
        path = hub.path
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh.read().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["user"], "落盘")
        self.assertEqual(lines[1]["seq"], 2)

    def test_ring_buffer_drops_oldest_but_counts_it(self):
        hub = console_hub.Hub(directory=self.tmp, record=False, max_turns=3)
        for i in range(5):
            hub.record({"i": i})
        kept = hub.since(0, 0)["turns"]
        self.assertEqual([t["i"] for t in kept], [2, 3, 4])
        self.assertEqual(hub.counts()["turns"], 5)
        self.assertGreaterEqual(hub.since(0, 0)["dropped"], 2)

    def test_log_lines_are_incremental(self):
        hub = console_hub.hub()
        hub.log(["a", "b"])
        hub.log(["c"])
        got = hub.since(0, 0)["logs"]
        self.assertEqual([x["text"] for x in got], ["a", "b", "c"])
        tail = hub.since(0, 2)["logs"]
        self.assertEqual([x["text"] for x in tail], ["c"])

    def test_record_can_be_disabled(self):
        hub = console_hub.Hub(directory=self.tmp, record=False)
        hub.record({"x": 1})
        self.assertIsNone(hub.path)
        self.assertEqual(len(hub.since(0, 0)["turns"]), 1)


# --------------------------------------------------------------------------
class SnapshotTest(_HubCase):
    def _turn(self):
        from pipeline import Timers
        timers = Timers()
        timers.marks = {"asr": 320, "first_token": 780, "first_sentence": 1100,
                        "first_tts_done": 1700, "first_byte": 1900}
        return timers, _FakeTurn(7, "你好", timers, "dev1")

    def test_extracts_marks_stats_and_memory(self):
        timers, turn = self._turn()
        snap = console_hub.snapshot(timers, turn, "你好", "device", pcm_bytes=6144)
        self.assertEqual(snap["marks"]["first_byte"], 1900)
        self.assertEqual(snap["first_audio_ms"], 1900)
        self.assertEqual(snap["audio_ms"], 6144 // 32)
        self.assertEqual(snap["pcm_bytes"], 6144)
        self.assertEqual(snap["source"], "device")
        self.assertEqual(snap["session"], "dev1")
        self.assertEqual(snap["engine"], "cosyvoice")
        self.assertEqual(snap["memory"]["l1"], 2)
        self.assertFalse(snap["aborted"])

    def test_aborted_turn_is_flagged(self):
        timers, turn = self._turn()
        turn.abort.set()
        snap = console_hub.snapshot(timers, turn, None, "text", 0)
        self.assertTrue(snap["aborted"])
        self.assertEqual(snap["user"], "你好")

    def test_non_json_stats_do_not_break_record(self):
        timers, turn = self._turn()
        turn.stats["attempts"] = object()
        snap = console_hub.snapshot(timers, turn, "x")
        json.dumps(snap)  # 必须可序列化
        self.assertIsInstance(snap["attempts"], str)


# --------------------------------------------------------------------------
class ReportTest(_HubCase):
    def test_report_lines_keep_historical_shape(self):
        from pipeline import Timers
        timers = Timers()
        timers.marks = {"asr": 300, "first_token": 700, "first_byte": 2000}
        turn = _FakeTurn(3, "你好", timers)
        turn.stats["attempts"] = ["deepseek"]
        lines = server.build_report(timers, turn, "你好")

        self.assertEqual(lines[0], "=== turn gen=3 ===")
        self.assertEqual(lines[1], "  user: 你好")
        self.assertIn("  ASR完成", "\n".join(lines))
        asr = [x for x in lines if x.strip().startswith("ASR完成")]
        self.assertEqual(len(asr), 1)
        self.assertTrue(asr[0].strip().endswith("300 ms"))
        self.assertIn("  FIRST_AUDIO_MS = 2000", lines)
        self.assertTrue(any(x.startswith("  engine=cosyvoice") for x in lines))
        self.assertTrue(any(x.startswith("  total=0.9s") for x in lines))
        self.assertTrue(any(x.startswith("  brain=deepseek") for x in lines))

    def test_missing_marks_print_dash(self):
        from pipeline import Timers
        timers = Timers()
        turn = _FakeTurn(1, "x", timers)
        lines = server.build_report(timers, turn, None)
        asr = [x for x in lines if x.strip().startswith("ASR完成")]
        self.assertEqual(len(asr), 1)
        self.assertTrue(asr[0].strip().endswith("-"))
        self.assertFalse(any(x.startswith("  user:") for x in lines))

    def test_report_feeds_hub_once(self):
        from pipeline import Timers
        timers = Timers()
        timers.marks = {"asr": 100, "first_byte": 900}
        turn = _FakeTurn(9, "hi", timers)
        with patch("builtins.print"):
            server.report(timers, turn, "hi", source="text", pcm_bytes=3200)
        data = console_hub.hub().since(0, 0)
        self.assertEqual(len(data["turns"]), 1)
        self.assertEqual(data["turns"][0]["source"], "text")
        self.assertEqual(data["turns"][0]["audio_ms"], 100)
        self.assertTrue(data["logs"])

    def test_hub_failure_never_raises(self):
        from pipeline import Timers
        timers = Timers()
        turn = _FakeTurn(1, "x", timers)
        with patch.object(console_hub.Hub, "record", side_effect=RuntimeError("boom")), \
                patch("builtins.print"):
            server.report(timers, turn, "x")  # 不应抛出


# --------------------------------------------------------------------------
class ConsoleRouteTest(_HubCase):
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
        super().setUp()
        self._saved = (server.Handler.bridge, server.Handler.asr)
        server.Handler.bridge = _FakeBridge()
        server.Handler.asr = None
        self._patch = patch.object(usage, "record_turn", return_value=None)
        self._patch.start()
        self._price = patch.object(llm, "profile_prices", return_value=(None, None))
        self._price.start()

    def tearDown(self):
        self._price.stop()
        self._patch.stop()
        server.Handler.bridge, server.Handler.asr = self._saved
        super().tearDown()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path, headers=None):
        import requests
        return requests.get(self.url(path), headers=headers or {}, timeout=5)

    def post(self, path, payload=None, headers=None, data=None):
        import requests
        return requests.post(self.url(path), json=payload, data=data,
                             headers=headers or {}, timeout=5)

    # ---- /console -------------------------------------------------------
    def test_console_page_is_served_without_auth(self):
        with patch.object(config, "auth_token", return_value="secret"):
            resp = self.get("/console")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["Content-Type"])
        self.assertIn("voice-ai", resp.text)

    def test_console_injects_token_on_loopback(self):
        with patch.object(config, "auth_token", return_value="tok-abc"):
            resp = self.get("/console")
        self.assertIn('"tok-abc"', resp.text)
        self.assertNotIn("__BRIDGE_TOKEN__", resp.text)

    def test_console_page_has_brain_switch(self):
        """切 API 的下拉必须留在页面里；误删会让调试台失去切脑能力。"""
        resp = self.get("/console")
        self.assertIn('id="brainMenu"', resp.text)
        self.assertIn("switchBrain", resp.text)
        self.assertIn('api("/brain"', resp.text)
        # 只读展示：菜单必须默认收起，不能一进页面就铺一层浮层
        self.assertIn('id="brainMenu" hidden', resp.text)

    def test_unknown_get_is_404(self):
        resp = self.get("/nope")
        self.assertEqual(resp.status_code, 404)

    # ---- /api/state -----------------------------------------------------
    def test_state_requires_auth(self):
        with patch.object(config, "auth_token", return_value="secret"):
            resp = self.get("/api/state")
        self.assertEqual(resp.status_code, 401)

    def test_state_reports_brain_engine_memory(self):
        meta = {"deepseek": {"model": "deepseek-flash",
                             "price_in": 2.0, "price_out": 8.0}}
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(llm, "brain_status",
                             return_value={"active": "deepseek", "pinned": None,
                                           "cooling": {}}), \
                patch.object(llm, "profile_names", return_value=["deepseek"]), \
                patch.object(llm, "profile_meta", return_value=meta):
            resp = self.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["brain"]["active"], "deepseek")
        self.assertEqual(data["profiles"], ["deepseek"])
        self.assertEqual(data["profile_meta"], meta)
        # 切脑下拉只允许拿到展示信息，密钥一类字段绝不能出现在 state 里
        self.assertEqual(set(data["profile_meta"]["deepseek"]),
                         {"model", "price_in", "price_out"})
        self.assertTrue(data["engine"]["edge"]["ready"])
        # 肥鱼在时它是主引擎，CosyVoice 不再并发加载
        self.assertFalse(data["engine"]["cosyvoice"]["ready"])
        self.assertFalse(data["engine"]["cosyvoice"]["loading"])
        self.assertFalse(data["engine"]["feiyu"]["ready"])
        self.assertTrue(data["engine"]["feiyu"]["loading"])
        self.assertFalse(data["memory"]["enabled"])
        self.assertIn("sentence_first_min", data["config"])

    def test_state_survives_brain_config_failure(self):
        boom = RuntimeError("brains.toml 坏了")
        with patch.object(config, "auth_token", return_value=None), \
                patch.object(llm, "brain_status", side_effect=boom):
            resp = self.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("brains.toml", data["brain"]["error"])
        self.assertEqual(data["profiles"], [])
        self.assertEqual(data["profile_meta"], {})

    # ---- /api/turns -----------------------------------------------------
    def test_turns_returns_increments(self):
        hub = console_hub.hub()
        hub.record({"user": "a", "first_audio_ms": 100})
        hub.record({"user": "b", "first_audio_ms": 200})
        with patch.object(config, "auth_token", return_value=None):
            resp = self.get("/api/turns?since=0")
        data = resp.json()
        self.assertEqual([t["user"] for t in data["turns"]], ["a", "b"])
        self.assertEqual(data["cursor"]["turn_seq"], 2)

        with patch.object(config, "auth_token", return_value=None):
            resp2 = self.get("/api/turns?since=2")
        self.assertEqual(resp2.json()["turns"], [])

    # ---- /api/memory ----------------------------------------------------
    def test_memory_without_store_says_disabled(self):
        with patch.object(config, "auth_token", return_value=None):
            resp = self.get("/api/memory")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])

    # ---- /api/text ------------------------------------------------------
    def test_text_turn_streams_pcm_and_records(self):
        with patch.object(config, "auth_token", return_value=None):
            resp = self.post("/api/text", {"text": "你好"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Voice"), "s16le/16000/mono")
        self.assertEqual(len(resp.content), 6144)

        # report() 在终止 chunk 之后才入账，客户端读到 EOF 时可能还差一拍
        deadline = time.time() + 2.0
        data = console_hub.hub().since(0, 0)
        while not data["turns"] and time.time() < deadline:
            time.sleep(0.02)
            data = console_hub.hub().since(0, 0)
        self.assertEqual(len(data["turns"]), 1)
        turn = data["turns"][0]
        self.assertEqual(turn["source"], "text")
        self.assertEqual(turn["user"], "你好")
        self.assertEqual(turn["pcm_bytes"], 6144)

    def test_text_turn_rejects_empty(self):
        with patch.object(config, "auth_token", return_value=None):
            resp = self.post("/api/text", {"text": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_text_turn_rejects_bad_json(self):
        with patch.object(config, "auth_token", return_value=None):
            resp = self.post("/api/text", data=b"{not json}",
                             headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)

    def test_text_turn_requires_auth(self):
        with patch.object(config, "auth_token", return_value="secret"):
            resp = self.post("/api/text", {"text": "你好"})
        self.assertEqual(resp.status_code, 401)

    def test_stop_still_works(self):
        with patch.object(config, "auth_token", return_value=None):
            resp = self.post("/stop", {})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("stopped_gen", resp.json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
