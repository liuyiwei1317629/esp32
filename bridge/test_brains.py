import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from brains import BrainConfig, BrainError, BrainRouter, Profile, load_config


def profile(name, **kw):
    base = dict(name=name, base_url="http://" + name, model="model-" + name,
                max_tokens=64, timeout_s=8.0, first_token_timeout_s=3.0)
    base.update(kw)
    return Profile(**base)


def make_config(active="a", fallback=("b",)):
    profiles = {"a": profile("a"), "b": profile("b"), "c": profile("c")}
    return BrainConfig(active=active, fallback=tuple(fallback), profiles=profiles)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def ok(*pieces):
    return list(pieces)


def fail(message="boom", retryable=False):
    return BrainError(message, retryable=retryable)


class FakeStreamer:
    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __call__(self, prof, messages, on_content, abort):
        self.calls.append(prof.name)
        steps = self.script.get(prof.name) or []
        if not steps:
            raise AssertionError("unexpected call to %s" % prof.name)
        action = steps.pop(0)
        if callable(action):
            return action(prof, on_content)
        if isinstance(action, Exception):
            raise action
        for piece in action:
            on_content(piece)
        return {"first_token_seconds": 0.01, "total_seconds": 0.02,
                "chunks": len(action), "content_chars": sum(len(p) for p in action),
                "reasoning_chars": 0, "finish_reason": "stop",
                "usage": {"completion_tokens": 3}, "aborted": False, "done": True}


class LoadConfigTest(unittest.TestCase):
    def test_reads_profiles_fallback_and_extra_body(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "brains.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("""
[brain]
active = "deepseek"
fallback = ["kimi"]

[brain.profiles.deepseek]
base_url = "https://api.deepseek.com"
key_file = "~/.voice-ai/keys/deepseek"
model = "deepseek-flash"
max_tokens = 320
timeout_s = 8
first_token_timeout_s = 3
price_in_cny_per_mtok = 2.0
price_out_cny_per_mtok = 8.0
extra_body = { thinking = { type = "disabled" } }

[brain.profiles.kimi]
base_url = "https://api.moonshot.cn/v1"
key_file = ""
model = "moonshot-v1-8k"
""")
            cfg = load_config(path)
        self.assertEqual(cfg.active, "deepseek")
        self.assertEqual(cfg.fallback, ("kimi",))
        deepseek = cfg.profiles["deepseek"]
        self.assertEqual(deepseek.base_url, "https://api.deepseek.com")
        self.assertEqual(deepseek.model, "deepseek-flash")
        self.assertEqual(deepseek.max_tokens, 320)
        self.assertEqual(deepseek.first_token_timeout_s, 3.0)
        self.assertEqual(deepseek.price_in_cny_per_mtok, 2.0)
        self.assertEqual(deepseek.price_out_cny_per_mtok, 8.0)
        self.assertEqual(deepseek.key_file,
                         os.path.normpath(os.path.expanduser("~/.voice-ai/keys/deepseek")))
        self.assertEqual(deepseek.extra_body, {"thinking": {"type": "disabled"}})
        self.assertEqual(cfg.profiles["kimi"].key_file, "")
        self.assertIsNone(cfg.profiles["kimi"].price_in_cny_per_mtok)
        self.assertIsNone(cfg.profiles["kimi"].price_out_cny_per_mtok)

    def test_missing_file_uses_legacy_deepseek_profile(self):
        cfg = load_config(os.path.join(tempfile.gettempdir(), "no-such-brains.toml"))
        self.assertEqual(cfg.active, "deepseek")
        self.assertEqual(cfg.fallback, ())
        p = cfg.profiles["deepseek"]
        self.assertEqual(p.base_url, config.DEEPSEEK_BASE_URL)
        self.assertEqual(p.model, config.DEEPSEEK_MODEL)
        self.assertEqual(p.key_file, config.DEEPSEEK_KEY_FILE)

    def test_unknown_active_or_fallback_is_rejected(self):
        body = """
[brain]
active = "nope"
fallback = ["a"]

[brain.profiles.a]
base_url = "http://a"
model = "ma"
"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "brains.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            with self.assertRaises(BrainError):
                load_config(path)

        body = body.replace('active = "nope"', 'active = "a"').replace(
            'fallback = ["a"]', 'fallback = ["missing"]')
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "brains.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            with self.assertRaises(BrainError):
                load_config(path)

    def test_bad_price_is_rejected(self):
        body = """
[brain]
active = "a"

[brain.profiles.a]
base_url = "http://a"
model = "ma"
price_in_cny_per_mtok = "cheap"
"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "brains.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            with self.assertRaises(BrainError):
                load_config(path)

    def test_profile_key_file_is_read_not_logged(self):
        with tempfile.TemporaryDirectory() as td:
            key_path = os.path.join(td, "key")
            with open(key_path, "w", encoding="utf-8") as fh:
                fh.write("  test-key  \n")
            p = profile("a", key_file=key_path)
            self.assertEqual(p.key(), "test-key")
            missing = profile("b", key_file=os.path.join(td, "missing"))
            with self.assertRaises(BrainError):
                missing.key()


class RouterTest(unittest.TestCase):
    def make_router(self, script, fallback=("b",), clock=None, prober=None):
        streamer = FakeStreamer(script)
        router = BrainRouter(make_config(fallback=fallback), streamer,
                             prober=prober, clock=clock or Clock())
        return router, streamer

    def test_healthy_active_profile_is_used(self):
        router, streamer = self.make_router({"a": [ok("你", "好。")]})
        got = []
        stats = router.stream_reply([], got.append)
        self.assertEqual(got, ["你", "好。"])
        self.assertEqual(streamer.calls, ["a"])
        self.assertEqual(stats["profile"], "a")
        self.assertFalse(stats["fallback_used"])
        self.assertEqual(stats["attempts"], 1)

    def test_retryable_failure_retries_the_same_profile_once(self):
        router, streamer = self.make_router({"a": [fail(retryable=True), ok("好。")]})
        got = []
        stats = router.stream_reply([], got.append)
        self.assertEqual(got, ["好。"])
        self.assertEqual(streamer.calls, ["a", "a"])
        self.assertEqual(stats["profile"], "a")
        self.assertFalse(stats["fallback_used"])
        self.assertEqual(stats["attempts"], 2)
        self.assertFalse(router.is_cooling("a"))

    def test_auth_failure_switches_without_retry(self):
        router, streamer = self.make_router({"a": [fail("401", retryable=False)],
                                             "b": [ok("好。")]})
        got = []
        stats = router.stream_reply([], got.append)
        self.assertEqual(got, ["好。"])
        self.assertEqual(streamer.calls, ["a", "b"])
        self.assertEqual(stats["profile"], "b")
        self.assertTrue(stats["fallback_used"])
        self.assertEqual(stats["attempts"], 2)
        self.assertTrue(router.is_cooling("a"))

    def test_retryable_failure_exhausted_then_fallback(self):
        router, streamer = self.make_router({"a": [fail(retryable=True), fail(retryable=True)],
                                             "b": [ok("好。")]})
        stats = router.stream_reply([], lambda _t: None)
        self.assertEqual(streamer.calls, ["a", "a", "b"])
        self.assertEqual(stats["profile"], "b")
        self.assertEqual(stats["attempts"], 3)
        self.assertTrue(router.is_cooling("a"))

    def test_never_switches_after_content_was_emitted(self):
        def emit_then_fail(prof, on_content):
            on_content("你")
            raise fail("mid-stream", retryable=True)

        router, streamer = self.make_router({"a": [emit_then_fail], "b": [ok("不该出现。")]})
        got = []
        with self.assertRaises(BrainError):
            router.stream_reply([], got.append)
        self.assertEqual(got, ["你"])
        self.assertEqual(streamer.calls, ["a"])
        self.assertTrue(router.is_cooling("a"))

    def test_cooling_profile_is_skipped_until_timeout(self):
        clock = Clock()
        router, streamer = self.make_router({
            "a": [fail("401"), ok("恢复了。")],
            "b": [ok("第一条。"), ok("第二条。")],
        }, clock=clock)
        router.stream_reply([], lambda _t: None)
        clock.advance(1)
        router.stream_reply([], lambda _t: None)
        self.assertEqual(streamer.calls, ["a", "b", "b"])
        clock.advance(301)
        router.stream_reply([], lambda _t: None)
        self.assertEqual(streamer.calls, ["a", "b", "b", "a"])
        self.assertFalse(router.is_cooling("a"))

    def test_probe_clears_cooldown(self):
        router, streamer = self.make_router({
            "a": [fail("401"), ok("回来了。")],
            "b": [ok("先顶一下。")],
        }, prober=lambda prof: True)
        router.stream_reply([], lambda _t: None)
        self.assertTrue(router.is_cooling("a"))
        self.assertTrue(router.probe_now("a"))
        self.assertFalse(router.is_cooling("a"))
        router.stream_reply([], lambda _t: None)
        self.assertEqual(streamer.calls, ["a", "b", "a"])

    def test_all_profiles_cooling_raises_without_calling_anyone(self):
        router, streamer = self.make_router({"a": [fail("401")], "b": [fail("401")]})
        with self.assertRaises(BrainError):
            router.stream_reply([], lambda _t: None)
        self.assertEqual(streamer.calls, ["a", "b"])
        with self.assertRaises(BrainError):
            router.stream_reply([], lambda _t: None)
        self.assertEqual(streamer.calls, ["a", "b"])

    def test_set_active_rejects_unknown_and_switches_next_turn(self):
        router, streamer = self.make_router({"a": [ok("a。")], "b": [ok("b。")]})
        with self.assertRaises(ValueError):
            router.set_active("missing")
        self.assertEqual(router.set_active("b"), "b")
        stats = router.stream_reply([], lambda _t: None)
        self.assertEqual(streamer.calls, ["b"])
        self.assertEqual(stats["profile"], "b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
