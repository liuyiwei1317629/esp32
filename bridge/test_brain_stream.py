import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm
from brains import BrainError, Profile


def sse(obj):
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def make_handler(responder, record):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            record["payload"] = json.loads(body)
            record["auth"] = self.headers.get("Authorization")
            record["path"] = self.path
            responder(self)

    return Handler


class LocalServer:
    def __init__(self, responder, record):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(responder, record))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class StreamProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_file = os.path.join(self.tmp.name, "key")
        with open(self.key_file, "w", encoding="utf-8") as fh:
            fh.write("test-key\n")

    def tearDown(self):
        self.tmp.cleanup()

    def prof(self, port, **kw):
        base = dict(name="local", base_url="http://127.0.0.1:%d" % port,
                    model="test-model", max_tokens=9, timeout_s=5.0,
                    first_token_timeout_s=1.0, key_file=self.key_file,
                    extra_body={"thinking": {"type": "disabled"}})
        base.update(kw)
        return Profile(**base)

    def test_streams_content_usage_and_sends_config(self):
        record = {}

        def responder(h):
            body = (sse({"choices": [{"delta": {"content": "你"}}]})
                    + sse({"choices": [{"delta": {"content": "好。"}, "finish_reason": "stop"}]})
                    + sse({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}})
                    + b"data: [DONE]\n\n")
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)

        got = []
        with LocalServer(responder, record) as server:
            stats = llm.stream_profile(self.prof(server.port), [], got.append)
        self.assertEqual(got, ["你", "好。"])
        self.assertTrue(stats["done"])
        self.assertEqual(stats["usage"], {"prompt_tokens": 7, "completion_tokens": 2})
        self.assertEqual(record["path"], "/chat/completions")
        self.assertEqual(record["auth"], "Bearer test-key")
        payload = record["payload"]
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["max_tokens"], 9)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_first_token_timeout_is_retryable(self):
        def responder(h):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Content-Length", "1000")
            h.end_headers()
            time.sleep(0.6)

        with LocalServer(responder, {}) as server:
            started = time.perf_counter()
            with self.assertRaises(BrainError) as ctx:
                llm.stream_profile(self.prof(server.port, first_token_timeout_s=0.1),
                                   [], lambda _t: None)
            elapsed = time.perf_counter() - started
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("first token", str(ctx.exception).lower())
        self.assertLess(elapsed, 0.5)

    def test_slow_headers_count_toward_first_token_timeout(self):
        def responder(h):
            time.sleep(0.6)
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Content-Length", "0")
            h.end_headers()

        with LocalServer(responder, {}) as server:
            started = time.perf_counter()
            with self.assertRaises(BrainError) as ctx:
                llm.stream_profile(self.prof(server.port, first_token_timeout_s=0.1),
                                   [], lambda _t: None)
            elapsed = time.perf_counter() - started
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("first token", str(ctx.exception).lower())
        self.assertLess(elapsed, 0.5)

    def test_http_401_is_not_retryable(self):
        def responder(h):
            body = b'{"error":"bad key"}'
            h.send_response(401)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)

        with LocalServer(responder, {}) as server:
            with self.assertRaises(BrainError) as ctx:
                llm.stream_profile(self.prof(server.port), [], lambda _t: None)
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.status, 401)

    def test_mid_stream_disconnect_is_retryable_and_keeps_emitted_text(self):
        def responder(h):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            chunk = sse({"choices": [{"delta": {"content": "你"}}]})
            h.wfile.write(("%X\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")
            h.wfile.flush()
            h.close_connection = True

        got = []
        with LocalServer(responder, {}) as server:
            with self.assertRaises(BrainError) as ctx:
                llm.stream_profile(self.prof(server.port), [], got.append)
        self.assertEqual(got, ["你"])
        self.assertTrue(ctx.exception.retryable)

    def test_missing_key_fails_before_any_request(self):
        prof = self.prof(1, key_file=os.path.join(self.tmp.name, "missing"))
        with self.assertRaises(BrainError) as ctx:
            llm.stream_profile(prof, [], lambda _t: None)
        self.assertIn("key file", str(ctx.exception).lower())
        self.assertFalse(ctx.exception.retryable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
