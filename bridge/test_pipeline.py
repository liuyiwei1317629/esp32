import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm
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
install_stream(["今天", "天气", "真好。", "我们", "一起", "出去玩", "吧。"])
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

print("pipeline: 6/6 passed")
