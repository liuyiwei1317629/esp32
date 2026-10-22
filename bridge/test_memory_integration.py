import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import context_builder
import llm
import memory_context
import memory_score
import memory_store
import memory_writer
import pipeline
import server
import usage

NOW = datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc)


class FakeTts:
    def synthesize(self, text):
        return b"\x01\x00" * 80, {
            "engine": "fake", "tts_ms": 1, "samples": 80, "fallback": False,
        }


class ExplodingStore:
    def active_facts(self):
        raise AssertionError("memory store must not be read when disabled")

    def active_episodes(self):
        raise AssertionError("memory store must not be read when disabled")

    def load_session(self, session_id):
        raise AssertionError("memory store must not be read when disabled")


class BrokenStore:
    def active_facts(self):
        raise RuntimeError("boom")


class RecordingStore(memory_store.MemoryStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loaded_sessions = []

    def load_session(self, session_id):
        self.loaded_sessions.append(session_id)
        return super().load_session(session_id)


def recording_stream(seen, parts=("好的。",)):
    def stream(messages, on_content, abort=None):
        seen.append([dict(item) for item in messages])
        for piece in parts:
            on_content(piece)
        return {
            "first_token_seconds": 0.001,
            "chunks": len(parts),
            "content_chars": sum(len(piece) for piece in parts),
            "usage": {},
            "finish_reason": "stop",
            "total_seconds": 0.001,
        }

    return stream


def snapshot(root):
    result = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                result[os.path.relpath(path, root)] = fh.read()
    return result


def run_turn(bridge, text, session_id="anon"):
    turn = bridge.begin(text, pipeline.Timers(), session_id=session_id)
    return turn, list(turn.pcm_chunks())


def make_store(root, session_id="dev1"):
    store = memory_store.MemoryStore(root, clock=lambda: NOW)
    store.upsert_fact("name", "用户叫小林", slot="name", pinned=True, now=NOW)
    store.upsert_episode("todo", "用户今天要交设计稿",
                         slot="todo:设计稿", now=NOW)
    store.save_session(session_id, {
        "summary": "最近在赶设计稿",
        "sticky_goal": "交设计稿",
        "recent_turns": [{
            "user": "在忙吗", "assistant": "在的",
            "ts": "2026-10-23T01:00:00+00:00",
        }],
        "completed_turns": 1,
    }, now=NOW)
    return store


class MemoryTurnIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_enabled_turn_injects_memory_with_500_char_input(self):
        store = make_store(self.root)
        seen = []
        text = "设计稿" + "嗯" * 497
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(config, "MEMORY_DIR", self.root), \
                mock.patch.object(config, "MEMORY_INPUT_BUDGET_TOKENS", 1050), \
                mock.patch.object(config, "MEMORY_CURRENT_INPUT_MAX_CHARS", 500), \
                mock.patch.object(llm, "stream_reply", recording_stream(seen)):
            bridge = pipeline.Bridge(
                FakeTts(), memory_store=store,
                token_cache=memory_score.TokenCache())
            turn, blocks = run_turn(bridge, text, "dev1")
        self.assertEqual(len(seen), 1)
        messages = seen[0]
        system = messages[0]["content"]
        for expected in ("用户叫小林", "用户今天要交设计稿", "最近在赶设计稿"):
            self.assertIn(expected, system)
        self.assertEqual(messages[-1]["content"], text)
        self.assertEqual(len(messages[-1]["content"]), 500)
        total = sum(context_builder.estimate_tokens(item["content"])
                    for item in messages)
        self.assertLessEqual(total, 1050)
        self.assertTrue(turn.completed)
        self.assertEqual(turn.reply_text, "好的。")
        self.assertTrue(blocks)

    def test_disabled_turn_uses_exact_legacy_messages(self):
        seen = []
        text = "你好"
        with mock.patch.object(config, "MEMORY_ENABLED", False), \
                mock.patch.object(llm, "stream_reply", recording_stream(seen)):
            bridge = pipeline.Bridge(
                FakeTts(), memory_store=ExplodingStore(),
                token_cache=memory_score.TokenCache())
            turn, _blocks = run_turn(bridge, text, "dev1")
        self.assertEqual(seen[0], [
            {"role": "system", "content": config.DEFAULT_PERSONA},
            {"role": "user", "content": text},
        ])
        self.assertTrue(turn.completed)

    def test_enabled_turn_does_not_write_memory_files(self):
        store = make_store(self.root)
        before = snapshot(self.root)
        seen = []
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(llm, "stream_reply", recording_stream(seen)):
            bridge = pipeline.Bridge(
                FakeTts(), memory_store=store,
                token_cache=memory_score.TokenCache())
            run_turn(bridge, "设计稿", "dev1")
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "state.lock")))

    def test_store_failure_falls_back_to_legacy_messages(self):
        seen = []
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(llm, "stream_reply", recording_stream(seen)):
            bridge = pipeline.Bridge(
                FakeTts(), memory_store=BrokenStore(),
                token_cache=memory_score.TokenCache())
            turn, _blocks = run_turn(bridge, "你好", "dev1")
        self.assertEqual(seen[0], [
            {"role": "system", "content": config.DEFAULT_PERSONA},
            {"role": "user", "content": "你好"},
        ])
        self.assertIsNone(turn.error)
        self.assertTrue(turn.completed)

    def test_interrupted_turn_is_not_completed_and_writes_nothing(self):
        store = make_store(self.root)
        before = snapshot(self.root)
        started = threading.Event()

        def slow_stream(messages, on_content, abort=None):
            started.set()
            for _ in range(400):
                if abort is not None and abort.is_set():
                    break
                time.sleep(0.005)
            return {
                "first_token_seconds": 0, "chunks": 0, "content_chars": 0,
                "usage": {}, "finish_reason": "stop", "total_seconds": 0,
            }

        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(llm, "stream_reply", slow_stream):
            bridge = pipeline.Bridge(
                FakeTts(), memory_store=store,
                token_cache=memory_score.TokenCache())
            turn = bridge.begin("设计稿", pipeline.Timers(),
                                session_id="dev1")
            self.assertTrue(started.wait(2))
            bridge.interrupt()
            self.assertEqual(list(turn.pcm_chunks()), [])
        self.assertFalse(turn.completed)
        self.assertEqual(turn.reply_text, "")
        self.assertEqual(snapshot(self.root), before)

    def test_new_generation_leaves_older_turn_incomplete(self):
        started = threading.Event()
        calls = []

        def stream(messages, on_content, abort=None):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                for _ in range(400):
                    if abort is not None and abort.is_set():
                        break
                    time.sleep(0.005)
                return {
                    "first_token_seconds": 0, "chunks": 0,
                    "content_chars": 0, "usage": {},
                    "finish_reason": "stop", "total_seconds": 0,
                }
            on_content("好的。")
            return {
                "first_token_seconds": 0.001, "chunks": 1,
                "content_chars": 3, "usage": {},
                "finish_reason": "stop", "total_seconds": 0.001,
            }

        store = make_store(self.root)
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(llm, "stream_reply", stream):
            bridge = pipeline.Bridge(
                FakeTts(), memory_store=store,
                token_cache=memory_score.TokenCache())
            older = bridge.begin("第一问", pipeline.Timers(),
                                 session_id="dev1")
            self.assertTrue(started.wait(2))
            newer = bridge.begin("第二问", pipeline.Timers(),
                                 session_id="dev1")
            list(older.pcm_chunks())
            list(newer.pcm_chunks())
        self.assertFalse(older.completed)
        self.assertTrue(newer.completed)

    def test_session_bucket_ignores_header_value(self):
        class Dummy:
            path = "/turn?session_id=dev1"

        header_value = server.Handler._session(Dummy())
        memory_sid = memory_store.parse_session_id(Dummy.path)
        # bb96 may fix _session() to "dev1"; the contract here is only that the
        # header exists and the memory bucket does not depend on its value.
        self.assertTrue(header_value)
        self.assertEqual(memory_sid, "dev1")
        self.assertNotEqual(memory_store.session_bucket(header_value),
                            memory_store.session_bucket(memory_sid))

        store = RecordingStore(self.root, clock=lambda: NOW)
        seen = []
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(llm, "stream_reply", recording_stream(seen)):
            bridge = pipeline.Bridge(
                FakeTts(), memory_store=store,
                token_cache=memory_score.TokenCache())
            run_turn(bridge, "你好", memory_sid)
        self.assertIn("dev1", store.loaded_sessions)
        self.assertNotIn("d=dev1", store.loaded_sessions)

    def test_malicious_session_id_is_hashed_under_sessions(self):
        store = make_store(self.root)
        sid = memory_store.parse_session_id(
            "/turn?session_id=..%2F..%2Fevil")
        self.assertEqual(sid, "../../evil")
        path = store.session_path(sid)
        self.assertEqual(os.path.dirname(path), store.sessions_dir)
        self.assertEqual(os.path.basename(path),
                         memory_store.session_bucket(sid) + ".json")
        before = snapshot(self.root)
        seen = []
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(llm, "stream_reply", recording_stream(seen)):
            bridge = pipeline.Bridge(
                FakeTts(), memory_store=store,
                token_cache=memory_score.TokenCache())
            run_turn(bridge, "你好", sid)
        self.assertEqual(snapshot(self.root), before)

    def test_debug_output_has_counts_but_no_memory_text(self):
        store = make_store(self.root)
        seen = []
        out = io.StringIO()
        private = "这是一句不该出现在调试输出里的用户原话"
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(config, "MEMORY_DEBUG", True), \
                mock.patch.object(llm, "stream_reply", recording_stream(seen)), \
                contextlib.redirect_stdout(out):
            bridge = pipeline.Bridge(
                FakeTts(), memory_store=store,
                token_cache=memory_score.TokenCache())
            run_turn(bridge, private, "dev1")
        rendered = out.getvalue()
        for label in ("memory: L1=", "L2=", "L3=", "L4=", "total=",
                      "trimmed="):
            self.assertIn(label, rendered)
        self.assertNotIn(private, rendered)
        self.assertNotIn("用户叫小林", rendered)

    def test_build_messages_accepts_current_input_limit(self):
        messages, info = memory_context.build_messages(
            "人格", [], "", "", [], [], "字" * 300,
            current_input_max_chars=200,
        )
        self.assertEqual(len(messages[-1]["content"]), 200)
        self.assertEqual(info["current_chars"], 200)


class ServerWriteIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.store = memory_store.MemoryStore(self.root, clock=lambda: NOW)
        self.writer = None

    def tearDown(self):
        if self.writer is not None:
            self.writer.stop()

    def handler(self, bridge):
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/turn?session_id=dev1"
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 5000)
        handler.close_connection = False
        handler.command = "POST"
        handler.requestline = "POST /turn?session_id=dev1 HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        return handler

    def serve_turn(self, text, dry_run, parts=("知道啦。",)):
        writer = memory_writer.MemoryWriter(
            self.store, dry_run=dry_run, clock=lambda: NOW)
        self.writer = writer
        bridge = pipeline.Bridge(
            FakeTts(), memory_store=self.store,
            token_cache=memory_score.TokenCache(), memory_writer=writer)

        class FakeAsr:
            def transcribe_wav(self, body):
                return text, 0.01

        cache_at_terminal = {}
        original_chunk = server.Handler._chunk

        def chunk(handler_self, data):
            if data == b"":
                cache_at_terminal["turns"] = writer.recent_cache.get("dev1")
            return original_chunk(handler_self, data)

        seen = []
        handler = self.handler(bridge)
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(config, "auth_token", return_value=None), \
                mock.patch.object(server.Handler, "bridge", bridge), \
                mock.patch.object(server.Handler, "asr", FakeAsr()), \
                mock.patch.object(server.Handler, "_chunk", chunk), \
                mock.patch.object(server, "report", return_value=None), \
                mock.patch.object(usage, "record_turn", return_value=None), \
                mock.patch.object(llm, "stream_reply",
                                  recording_stream(seen, parts)):
            handler.do_POST()
        self.assertTrue(writer.wait_idle())
        return writer, cache_at_terminal, seen

    def test_completed_turn_writes_before_terminal_chunk(self):
        writer, cache_at_terminal, _seen = self.serve_turn(
            "我叫小周", dry_run=False)

        self.assertEqual([item["user"] for item in cache_at_terminal["turns"]],
                         ["我叫小周"])
        self.assertEqual([item["text"] for item in self.store.active_facts()],
                         ["用户叫小周"])
        session = self.store.load_session("dev1")
        self.assertIn("我叫小周", session["summary"])
        self.assertEqual(session["completed_turns"], 1)
        self.assertEqual(writer.counters()["written_jobs"], 1)

        messages, info = memory_context.build_for_turn(
            self.store, "人格", "dev1", "我是谁",
            recent_cache=writer.recent_cache.get("dev1"))
        system = messages[0]["content"]
        self.assertIn("用户叫小周", system)
        self.assertIn("我叫小周", system)
        self.assertFalse(info["fallback"])

    def test_dry_run_turn_keeps_files_unchanged(self):
        before = snapshot(self.root)
        writer, cache_at_terminal, _seen = self.serve_turn(
            "我叫小林", dry_run=True)

        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.store.active_facts(), [])
        self.assertEqual([item["user"] for item in cache_at_terminal["turns"]],
                         ["我叫小林"])
        self.assertEqual(writer.counters()["dry_run_jobs"], 1)

    def test_sensitive_turn_is_not_cached_or_written(self):
        before = snapshot(self.root)
        writer, cache_at_terminal, _seen = self.serve_turn(
            "我的手机号是13800138000", dry_run=True)

        self.assertEqual(cache_at_terminal["turns"], [])
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(writer.counters()["rejected_sensitive"], 1)


class ServerMemoryWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def handler(self, path="/turn?session_id=dev1", bridge=None, asr=None):
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 5000)
        handler.close_connection = False
        handler.command = "POST"
        handler.requestline = "POST %s HTTP/1.1" % path
        handler.request_version = "HTTP/1.1"
        return handler

    def test_turn_passes_parsed_session_but_keeps_header(self):
        class FakeTurn:
            gen = 9
            error = None
            stats = {}

            def __init__(self):
                self.abort = threading.Event()

            def pcm_chunks(self):
                return iter(())

        class FakeBridge:
            def __init__(self):
                self.calls = []

            def begin(self, text, timers, session_id="anon"):
                self.calls.append((text, session_id))
                return FakeTurn()

        class FakeAsr:
            def transcribe_wav(self, body):
                return "你好", 0.01

        bridge = FakeBridge()
        handler = self.handler(bridge=bridge)
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(config, "auth_token", return_value=None), \
                mock.patch.object(server.Handler, "bridge", bridge), \
                mock.patch.object(server.Handler, "asr", FakeAsr()), \
                mock.patch.object(server, "report", return_value=None), \
                mock.patch.object(usage, "record_turn", return_value=None):
            handler.do_POST()
        self.assertEqual(bridge.calls, [("你好", "dev1")])
        raw = handler.wfile.getvalue()
        self.assertIn(b"X-Session-Id:", raw)

    def test_turn_when_disabled_keeps_two_argument_begin(self):
        class FakeTurn:
            gen = 3
            error = None
            stats = {}

            def __init__(self):
                self.abort = threading.Event()

            def pcm_chunks(self):
                return iter(())

        class FakeBridge:
            def begin(self, text, timers):
                self.called = (text, timers)
                return FakeTurn()

        class FakeAsr:
            def transcribe_wav(self, body):
                return "你好", 0.01

        bridge = FakeBridge()
        handler = self.handler(bridge=bridge)
        with mock.patch.object(config, "MEMORY_ENABLED", False), \
                mock.patch.object(config, "auth_token", return_value=None), \
                mock.patch.object(server.Handler, "bridge", bridge), \
                mock.patch.object(server.Handler, "asr", FakeAsr()), \
                mock.patch.object(server, "report", return_value=None), \
                mock.patch.object(usage, "record_turn", return_value=None):
            handler.do_POST()
        self.assertEqual(bridge.called[0], "你好")

    def test_run_local_uses_local_session(self):
        class FakeTurn:
            gen = 4
            error = None
            stats = {}

            def __init__(self):
                self.abort = threading.Event()

            def pcm_chunks(self):
                yield b"\x01\x00" * 80

        class FakeBridge:
            def __init__(self):
                self.calls = []

            def begin(self, text, timers, session_id="anon"):
                self.calls.append((text, session_id))
                return FakeTurn()

        bridge = FakeBridge()
        args = SimpleNamespace(text="你好", file=None, play=False)
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(server, "OUT_DIR", self.root), \
                mock.patch.object(server, "report", return_value=None):
            server.run_local(args, bridge, None)
        self.assertEqual(bridge.calls, [("你好", "local")])

    def test_run_local_stops_writer_after_enqueue(self):
        class FakeWriter:
            def __init__(self):
                self.stops = 0

            def stop(self, timeout=memory_writer.SHUTDOWN_WAIT_SECONDS):
                self.stops += 1
                return True

        class FakeTurn:
            gen = 4
            error = None
            stats = {}

            def __init__(self):
                self.abort = threading.Event()

            def pcm_chunks(self):
                return iter(())

        class FakeBridge:
            def __init__(self):
                self.memory_writer = FakeWriter()

            def begin(self, text, timers, session_id="anon"):
                return FakeTurn()

        bridge = FakeBridge()
        args = SimpleNamespace(text="你好", file=None, play=False)
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(server, "report", return_value=None):
            server.run_local(args, bridge, None)
        self.assertEqual(bridge.memory_writer.stops, 1)

    def test_serve_stops_writer_and_closes_server_on_exit(self):
        self.addCleanup(setattr, server.Handler, "bridge", None)
        self.addCleanup(setattr, server.Handler, "asr", None)

        class FakeWriter:
            def __init__(self):
                self.stops = 0

            def stop(self, timeout=memory_writer.SHUTDOWN_WAIT_SECONDS):
                self.stops += 1
                return True

        class FakeServer:
            def __init__(self, address, handler):
                self.closed = False

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                self.closed = True

        writer = FakeWriter()
        bridge = SimpleNamespace(memory_writer=writer)
        fake = FakeServer((config.BRIDGE_HOST, config.BRIDGE_PORT),
                          server.Handler)
        with mock.patch.object(config, "auth_token", return_value=None), \
                mock.patch.object(server, "ThreadingHTTPServer",
                                  return_value=fake), \
                mock.patch.object(llm, "brain_status", return_value={}):
            server.serve(bridge, None)
        self.assertEqual(writer.stops, 1)
        self.assertTrue(fake.closed)

    def test_build_bridge_only_constructs_memory_when_enabled(self):
        with mock.patch.object(config, "MEMORY_ENABLED", False), \
                mock.patch.object(server.memory_store, "MemoryStore",
                                  side_effect=AssertionError("must not construct")), \
                mock.patch.object(server.memory_score, "TokenCache",
                                  side_effect=AssertionError("must not construct")):
            bridge = server.build_bridge(FakeTts())
        self.assertIsNone(bridge.memory_store)
        self.assertIsNone(bridge.token_cache)
        self.assertIsNone(bridge.memory_writer)

        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(config, "MEMORY_DIR", self.root):
            bridge = server.build_bridge(FakeTts())
        self.addCleanup(bridge.memory_writer.stop)
        self.assertEqual(bridge.memory_store.root, self.root)
        self.assertIsInstance(bridge.token_cache, memory_score.TokenCache)
        self.assertIsInstance(bridge.memory_writer, memory_writer.MemoryWriter)
        self.assertTrue(bridge.memory_writer.dry_run)

    def test_build_bridge_warns_when_schema_is_newer(self):
        with open(os.path.join(self.root, "state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"schema_version": memory_store.SCHEMA_VERSION + 1}, fh)
        out = io.StringIO()
        with mock.patch.object(config, "MEMORY_ENABLED", True), \
                mock.patch.object(config, "MEMORY_DIR", self.root), \
                contextlib.redirect_stdout(out):
            bridge = server.build_bridge(FakeTts())
        self.addCleanup(bridge.memory_writer.stop)
        self.assertTrue(bridge.memory_store.read_only)
        self.assertTrue(bridge.memory_writer.dry_run)
        rendered = out.getvalue()
        self.assertIn("WARN", rendered)
        self.assertIn("read-only", rendered)
        self.assertIn(str(memory_store.SCHEMA_VERSION + 1), rendered)


class MemoryConfigTest(unittest.TestCase):
    def test_read_path_settings_exist(self):
        self.assertIsInstance(config.MEMORY_ENABLED, bool)
        self.assertTrue(config.MEMORY_DIR)
        self.assertGreater(config.MEMORY_INPUT_BUDGET_TOKENS, 0)
        self.assertGreater(config.MEMORY_CURRENT_INPUT_MAX_CHARS, 0)
        self.assertIsInstance(config.MEMORY_DRY_RUN, bool)
        self.assertIsInstance(config.MEMORY_DEBUG, bool)

    def test_invalid_numeric_env_falls_back_at_import(self):
        env = dict(os.environ)
        env.update({
            "MEMORY_ENABLED": "0",
            "BRIDGE_PORT": "abc",
            "DEEPSEEK_MAX_TOKENS": "abc",
            "MEMORY_INPUT_BUDGET_TOKENS": "abc",
            "MEMORY_CURRENT_INPUT_MAX_CHARS": "abc",
            "DEEPSEEK_PRICE_IN_CNY_PER_MTOK": "abc",
            "DEEPSEEK_PRICE_OUT_CNY_PER_MTOK": "abc",
            "COSYVOICE_TIMEOUT": "abc",
        })
        code = (
            "import config;"
            "print(config.BRIDGE_PORT);"
            "print(config.DEEPSEEK_MAX_TOKENS);"
            "print(config.MEMORY_INPUT_BUDGET_TOKENS);"
            "print(config.MEMORY_CURRENT_INPUT_MAX_CHARS);"
            "print(config.DEEPSEEK_PRICE_IN_CNY_PER_MTOK);"
            "print(config.DEEPSEEK_PRICE_OUT_CNY_PER_MTOK);"
            "print(config.COSYVOICE_TIMEOUT);"
            "print(int(config.MEMORY_ENABLED))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [
            "8080", "320", "1050", "500", "2.0", "8.0", "9.0", "0",
        ])
        self.assertGreaterEqual(proc.stderr.count("WARN"), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
