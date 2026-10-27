import json
import os
import sys
import threading
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import local_plugin


def sse(**event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


def content(text, **extra):
    event = {"choices": [{"delta": {"content": text}}]}
    event.update(extra)
    return sse(**event)


def reasoning(text):
    return sse(choices=[{"delta": {"reasoning_content": text}}])


class FakeResponse:
    def __init__(self, lines, status_code=200, text=""):
        self._lines = lines
        self.status_code = status_code
        self.text = text
        self.closed = False

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class LocalPluginTest(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("LOCAL_PLUGIN_BASE_URL", "http://127.0.0.1:11434/v1"),
            ("LOCAL_PLUGIN_MODEL", "qwen3.5:2b-q4_K_M"),
            ("LOCAL_PLUGIN_MAX_TOKENS", 160),
            ("LOCAL_PLUGIN_TIMEOUT_S", 15.0),
        ):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_streams_content_with_functional_payload(self):
        resp = FakeResponse([
            content("好"),
            content("的。"),
            sse(choices=[], usage={"prompt_tokens": 12, "completion_tokens": 3}),
            b"data: [DONE]",
        ])
        got = []
        with patch.object(local_plugin.requests, "post", return_value=resp) as post:
            stats = local_plugin.stream_reply("现在怎么办", got.append)

        payload = post.call_args.kwargs["json"]
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertEqual(post.call_args.kwargs["timeout"], (3.0, 15.0))
        self.assertEqual(payload["model"], "qwen3.5:2b-q4_K_M")
        self.assertEqual(payload["max_tokens"], 160)
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["keep_alive"], 0)
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(payload["messages"], [
            {"role": "system", "content": local_plugin.LOCAL_SYSTEM_PROMPT},
            {"role": "user", "content": "现在怎么办"},
        ])
        self.assertNotEqual(local_plugin.LOCAL_SYSTEM_PROMPT, config.DEFAULT_PERSONA)
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

        self.assertEqual(got, ["好", "的。"])
        self.assertEqual(stats["profile"], "local")
        self.assertTrue(stats["fallback_used"])
        self.assertEqual(stats["usage"],
                         {"prompt_tokens": 12, "completion_tokens": 3})
        self.assertTrue(resp.closed)

    def test_reasoning_only_reply_is_an_error_not_empty_audio(self):
        resp = FakeResponse([reasoning("先想一想"), b"data: [DONE]"])
        got = []
        with patch.object(local_plugin.requests, "post", return_value=resp):
            with self.assertRaises(local_plugin.LocalPluginError):
                local_plugin.stream_reply("在吗", got.append)
        self.assertEqual(got, [])
        self.assertTrue(resp.closed)

    def test_http_error_is_reported_and_closed(self):
        resp = FakeResponse([], status_code=500, text="model not found")
        with patch.object(local_plugin.requests, "post", return_value=resp):
            with self.assertRaises(local_plugin.LocalPluginError) as caught:
                local_plugin.stream_reply("在吗", lambda _: None)
        self.assertIn("500", str(caught.exception))
        self.assertIn("model not found", str(caught.exception))
        self.assertTrue(resp.closed)

    def test_timeout_becomes_local_plugin_error(self):
        with patch.object(local_plugin.requests, "post",
                          side_effect=requests.Timeout("slow")):
            with self.assertRaises(local_plugin.LocalPluginError):
                local_plugin.stream_reply("在吗", lambda _: None)

    def test_connection_error_becomes_local_plugin_error(self):
        with patch.object(local_plugin.requests, "post",
                          side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(local_plugin.LocalPluginError):
                local_plugin.stream_reply("在吗", lambda _: None)

    def test_abort_stops_forwarding_without_raising(self):
        abort = threading.Event()
        got = []

        def collect(text):
            got.append(text)
            if text == "第二":
                abort.set()

        resp = FakeResponse([content("第一"), content("第二"), content("第三"),
                             b"data: [DONE]"])
        with patch.object(local_plugin.requests, "post", return_value=resp):
            stats = local_plugin.stream_reply("在吗", collect, abort=abort)

        self.assertEqual(got, ["第一", "第二"])
        self.assertTrue(stats["aborted"])
        self.assertFalse(stats["done"])
        self.assertEqual(stats["profile"], "local")


if __name__ == "__main__":
    unittest.main(verbosity=2)
