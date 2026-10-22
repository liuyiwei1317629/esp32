import json, os, sys, threading

sys.path.insert(0, os.path.dirname(__file__))
from llm import consume


def d(**kv):
    return ("data: " + json.dumps(kv, ensure_ascii=False)).encode("utf-8")


def content(t, **extra):
    ev = {"choices": [{"delta": {"content": t}}]}
    ev.update(extra)
    return d(**ev)


def reasoning(t):
    return d(choices=[{"delta": {"reasoning_content": t}}])


def run(lines, abort=None):
    got = []
    stats = consume(iter(lines), got.append, abort)
    return got, stats


# 1. normal stream, blank lines and the [DONE] terminator
got, s = run([content("你好"), content("，世界。"), b"", b"data: [DONE]"])
assert got == ["你好", "，世界。"], got
assert s["done"] and s["chunks"] == 2 and s["content_chars"] == 6, s

# 2. reasoning_content must never reach the TTS path
got, s = run([reasoning("让我先想想"), content("答案来了。"), b"data: [DONE]"])
assert got == ["答案来了。"], got
assert s["reasoning_chars"] == 5 and s["content_chars"] == 5, s

# 3. usage attached to the trailing chunk with empty choices
got, s = run([content("好。"),
              d(choices=[], usage={"completion_tokens": 9, "prompt_tokens": 20}),
              b"data: [DONE]"])
assert s["usage"] == {"completion_tokens": 9, "prompt_tokens": 20}, s["usage"]

# 4. usage attached to the last content chunk instead
got, s = run([content("甲"), content("乙", usage={"completion_tokens": 12}), b"data: [DONE]"])
assert s["usage"] == {"completion_tokens": 12}, s["usage"]

# 5. last non-empty usage wins
got, s = run([content("甲", usage={"a": 1}), content("乙"),
              d(choices=[], usage={"a": 2}), b"data: [DONE]"])
assert s["usage"] == {"a": 2}, s["usage"]

# 6. malformed JSON and non-data lines are skipped, not fatal
got, s = run([b": heartbeat", b"data: {not json", content(" survives"), b"data: [DONE]"])
assert got == [" survives"], got

# 7. abort stops the stream mid-sentence
ev = threading.Event()
seen = []


def feed(t):
    seen.append(t)
    if t == "第二":
        ev.set()


stats = consume(iter([content("第一"), content("第二"), content("第三"), b"data: [DONE]"]),
                feed, abort=ev)
assert seen == ["第一", "第二"], seen
assert stats["aborted"] and not stats["done"], stats

# 8. finish_reason is recorded so a truncated reply can be reported
got, s = run([d(choices=[{"delta": {"content": "完。"}, "finish_reason": "stop"}]),
              b"data: [DONE]"])
assert s["finish_reason"] == "stop", s

# 9. first_token_seconds is measured on the first content chunk
lines = [content("甲"), content("乙")]
clock_times = iter([0.0, 0.5, 1.4])
got = []
stats = consume(iter(lines), got.append, clock=lambda: next(clock_times))
assert stats["first_token_seconds"] == 0.5, stats

print("llm parse: 9/9 passed")
