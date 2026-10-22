import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("BRIDGE_HOST", "127.0.0.1")
os.environ.setdefault("BRIDGE_PORT", "8088")
sys.path.insert(0, HERE)

import imageio_ffmpeg
import numpy as np
import requests

import config
import llm

REPLY = ["我", "听到", "你讲", "嘅啦。", "今日", "天气", "确实", "唔错，",
         "要唔好", "一齐", "出去", "行下？"]


def fake_stream(messages, on_content, abort=None):
    stats = {"first_token_seconds": 0.03, "chunks": 0, "content_chars": 0,
             "usage": {"completion_tokens": 21}, "finish_reason": "stop",
             "total_seconds": 0.0, "reasoning_chars": 0, "aborted": False, "done": True}
    t0 = time.perf_counter()
    for piece in REPLY:
        if abort is not None and abort.is_set():
            stats["aborted"] = True
            break
        time.sleep(0.03)
        on_content(piece)
        stats["chunks"] += 1
        stats["content_chars"] += len(piece)
    stats["total_seconds"] = time.perf_counter() - t0
    return stats


llm.stream_reply = fake_stream

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import server
from asr_engine import Asr
from pipeline import Bridge
from tts import Tts


def make_input_wav():
    src = os.path.join(config.VOICE_ROOT, "test-data", "zh.mp3")
    dst = os.path.join(HERE, "out", "in_16k.wav")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", src, "-ac", "1", "-ar", str(config.SAMPLE_RATE), dst], check=True)
    return dst


tts = Tts(start_cosyvoice=False)
print("edge worker ready=%s warm_ms=%s" % (tts.edge.ready, tts.edge.info.get("warm_ms")))
t0 = time.perf_counter()
asr = Asr("cpu")
print("SenseVoice CPU load %.1f s" % (time.perf_counter() - t0))

bridge = Bridge(tts)
threading.Thread(target=server.serve, args=(bridge, asr), daemon=True).start()
time.sleep(1.0)

url = "http://127.0.0.1:%d" % config.BRIDGE_PORT
with open(make_input_wav(), "rb") as fh:
    wav = fh.read()

# 1. one round trip: WAV in, chunked PCM out, first-byte latency measured
t0 = time.perf_counter()
first_byte = None
received = bytearray()
with requests.post(url + "/turn?session_id=e2etest", data=wav, stream=True,
                   headers={"X-Session": "1"}, timeout=60) as resp:
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    assert resp.headers["X-Voice"] == "s16le/16000/mono", dict(resp.headers)
    gen_id = resp.headers["X-Gen-Id"]
    for block in resp.iter_content(4096):
        if first_byte is None:
            first_byte = time.perf_counter() - t0
        received += block
total = time.perf_counter() - t0

audio = np.frombuffer(bytes(received), dtype="<i2")
seconds = len(audio) / config.SAMPLE_RATE
print("\n[turn 1] gen=%s first_byte=%.2fs total=%.2fs audio=%.2fs peak=%d" % (
    gen_id, first_byte, total, seconds, int(np.abs(audio).max())))
assert seconds > 1.0, "too little audio"
assert int(np.abs(audio).max()) > 1000, "silence"

# 2. barge-in: /stop must cut the running turn short
t1 = time.perf_counter()
got = bytearray()
stop_at = None
with requests.post(url + "/turn?session_id=e2etest", data=wav, stream=True, timeout=60) as resp:
    for block in resp.iter_content(4096):
        got += block
        if stop_at is None and len(got) >= 8192:
            requests.post(url + "/stop", timeout=10)
            stop_at = time.perf_counter()
    closed_at = time.perf_counter()
assert stop_at is not None, "stream ended before the interrupt fired"
print("[turn 2] %d bytes then /stop, stream closed %.2fs later" % (
    len(got), closed_at - stop_at))
assert closed_at - stop_at < 2.0, "interrupt did not close the stream"
assert bridge.gen > int(gen_id), "generation id did not advance"

print("\ne2e OK: ASR(cpu) -> fake LLM -> split -> edge TTS -> chunked PCM")
tts.stop()
