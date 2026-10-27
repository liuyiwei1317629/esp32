import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import llm
import local_plugin
import pipeline
from pipeline import Bridge, Timers

CLOCK = time.perf_counter
events = []


def log(kind, detail):
    events.append((CLOCK() - _t0, kind, detail))


def fake_tts(delay):
    class Fake:
        def synthesize(self, text):
            log("tts_start", text)
            time.sleep(delay)
            log("tts_end", text)
            return (b"\x01\x00" * 80, {"engine": "fake", "tts_ms": int(delay * 1000),
                                       "samples": 80, "fallback": False})

    return Fake()


def install_stream(parts, gap=0.02):
    def stream(messages, on_content, abort=None):
        for piece in parts:
            time.sleep(gap)
            log("llm", piece)
            on_content(piece)
        return {"first_token_seconds": gap, "chunks": len(parts),
                "content_chars": sum(len(p) for p in parts), "usage": {"completion_tokens": 7},
                "finish_reason": "stop", "total_seconds": gap * len(parts)}

    llm.stream_reply = stream


_t0 = CLOCK()

# 1. a full turn produces audio and stamps every stage
# Sentences are >= 18 chars each, so the turn's splitter (config.SENTENCE_MIN_CHARS)
# really cuts two of them instead of merging.
install_stream(["今天的天气", "真的很不错，", "我们一起", "出去玩吧。",
                "明天我们还", "可以一起去公园里散步聊天吧。"])
bridge = Bridge(fake_tts(0.05))
turn = bridge.begin("今天天气怎么样", Timers())
chunks = list(turn.pcm_chunks())
assert turn.error is None, turn.error
assert len(chunks) == 2 * 4096 // 4096 or len(chunks) >= 2, len(chunks)
marks = turn.timers.marks
for key in ("first_token", "first_sentence", "first_tts_done", "first_byte"):
    assert key in marks, marks
assert marks["first_byte"] >= marks["first_tts_done"], marks

# 2. the three stages really overlap: LLM is still talking while TTS works
starts = [t for t, k, d in events if k == "tts_start"]
llm_late = [t for t, k, d in events if k == "llm" and t > starts[0]]
assert llm_late, "no overlap, pipeline is serial"
assert starts[0] < marks["first_byte"] / 1000.0, "TTS started after audio came out"

# 3. a reply with no closing punctuation still gets spoken (flush, not dropped)
events.clear()
install_stream(["这句话没有", "结尾标点"])
bridge = Bridge(fake_tts(0.01))
turn = bridge.begin("随便聊", Timers())
chunks = list(turn.pcm_chunks())
assert turn.error is None, turn.error
assert chunks, "flushed tail sentence produced nothing"

# 4. interrupt kills the old generation's audio stream
install_stream(["一", "二", "三", "四", "五", "六"], gap=0.05)
bridge = Bridge(fake_tts(0.15))
turn = bridge.begin("打断测试", Timers())
first = next(turn.pcm_chunks())
bridge.interrupt()
assert list(turn.pcm_chunks()) == [], "stale audio kept flowing after interrupt"
assert bridge.superseded(turn.gen) and first

# 5. a new turn supersedes the previous one by generation id
install_stream(["甲甲甲甲甲甲。乙乙乙乙乙乙。"], gap=0.01)
bridge = Bridge(fake_tts(0.01))
older = bridge.begin("第一问", Timers())
newer = bridge.begin("第二问", Timers())
assert newer.gen == older.gen + 1, (older.gen, newer.gen)
assert older.abort.is_set()
assert not bridge.superseded(newer.gen)

# 6. a rules-only command answers locally and never calls the LLM
def failing_stream(messages, on_content, abort=None):
    raise AssertionError("local command must not call the LLM")


llm.stream_reply = failing_stream
bridge = Bridge(fake_tts(0.01))
turn = bridge.begin("现在几点", Timers())
chunks = list(turn.pcm_chunks())
assert turn.error is None, turn.error
assert turn.local_command == "time", turn.local_command
assert turn.reply_text.startswith("现在是"), turn.reply_text
assert chunks, "local time reply produced no audio"

# 7. with the local plugin off, a cloud failure keeps the existing error path
def cloud_down(messages, on_content, abort=None):
    raise RuntimeError("cloud down")


def must_not_run(text, on_content, abort=None):
    raise AssertionError("local plugin must not run here")


llm.stream_reply = cloud_down
local_plugin.stream_reply = must_not_run
old_enabled = config.LOCAL_PLUGIN_ENABLED
config.LOCAL_PLUGIN_ENABLED = False
try:
    bridge = Bridge(fake_tts(0.01))
    turn = bridge.begin("随便聊", Timers())
    chunks = list(turn.pcm_chunks())
finally:
    config.LOCAL_PLUGIN_ENABLED = old_enabled
assert chunks == [], "disabled local plugin produced audio"
assert turn.error and "cloud down" in turn.error, turn.error
assert not turn.local_plugin

# 8. when every cloud profile fails before any text, the local model answers
local_calls = []


def local_ok(text, on_content, abort=None):
    local_calls.append(text)
    on_content("本地可以做简单回答。")
    return {"profile": "local", "fallback_used": True, "chunks": 1,
            "content_chars": 10, "finish_reason": "stop",
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            "total_seconds": 0.1}


llm.stream_reply = cloud_down
local_plugin.stream_reply = local_ok
config.LOCAL_PLUGIN_ENABLED = True
try:
    bridge = Bridge(fake_tts(0.01))
    turn = bridge.begin("讲个笑话", Timers())
    chunks = list(turn.pcm_chunks())
finally:
    config.LOCAL_PLUGIN_ENABLED = old_enabled
assert chunks, "local fallback produced no audio"
assert turn.error is None, turn.error
assert turn.local_plugin and not turn.local_plugin_failed
assert turn.stats["profile"] == "local"
assert turn.stats["chunks"] == 1
assert turn.reply_text == "本地可以做简单回答。"
assert local_calls == ["讲个笑话"]
assert "local_fallback" in turn.timers.marks, turn.timers.marks

# 9. once the cloud has already spoken, the local model must not take over
def half_then_fail(messages, on_content, abort=None):
    on_content("云端已经说了半句")
    raise RuntimeError("cloud stream dropped")


llm.stream_reply = half_then_fail
local_plugin.stream_reply = must_not_run
config.LOCAL_PLUGIN_ENABLED = True
try:
    bridge = Bridge(fake_tts(0.01))
    turn = bridge.begin("讲个故事", Timers())
    chunks = list(turn.pcm_chunks())
finally:
    config.LOCAL_PLUGIN_ENABLED = old_enabled
assert chunks == [], "half a cloud reply was mixed with local audio"
assert turn.error and "cloud stream dropped" in turn.error, turn.error
assert not turn.local_plugin

# 10. when the local model fails too, speak one fixed sentence and no error
def local_down(text, on_content, abort=None):
    raise RuntimeError("ollama down")


llm.stream_reply = cloud_down
local_plugin.stream_reply = local_down
config.LOCAL_PLUGIN_ENABLED = True
try:
    bridge = Bridge(fake_tts(0.01))
    turn = bridge.begin("你在吗", Timers())
    chunks = list(turn.pcm_chunks())
finally:
    config.LOCAL_PLUGIN_ENABLED = old_enabled
assert chunks, "fixed fallback sentence produced no audio"
assert turn.error is None, turn.error
assert turn.local_plugin and turn.local_plugin_failed
assert turn.reply_text == local_plugin.FALLBACK_REPLY
assert turn.stats["profile"] == "local"

print("pipeline: 10/10 passed")
