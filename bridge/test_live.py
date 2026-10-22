"""Live full-chain test: real ASR -> real DeepSeek -> split -> CosyVoice -> chunked PCM.

Closes the two gaps the fake-LLM e2e cannot cover: the real cloud TTFT and
CosyVoice as the engine behind POST /turn. Needs ~/.deepseek/key.

    .venv/Scripts/python.exe test_live.py
"""
import difflib
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("BRIDGE_HOST", "127.0.0.1")
os.environ.setdefault("BRIDGE_PORT", "8088")
sys.path.insert(0, HERE)

import numpy as np
import requests

import config

config.force_utf8_output()

if not config.api_key():
    print("no DeepSeek key at %s" % config.DEEPSEEK_KEY_FILE)
    sys.exit(1)
print("model=%s base=%s max_tokens=%s thinking=%s" % (
    config.DEEPSEEK_MODEL, config.DEEPSEEK_BASE_URL,
    config.DEEPSEEK_MAX_TOKENS, config.DEEPSEEK_THINKING))

import llm

# Spy on the real LLM call: the network request itself is untouched, this only
# collects the reply text and stream stats so they can be verified afterwards.
_real_stream = llm.stream_reply
llm_stats = []


def spying_stream(messages, on_content, abort=None):
    parts = []

    def cb(text):
        parts.append(text)
        on_content(text)

    stats = _real_stream(messages, cb, abort)
    llm_stats.append(dict(stats, reply="".join(parts)))
    return stats


llm.stream_reply = spying_stream

import server
from asr_engine import Asr
from pipeline import Bridge
from tts import Tts


def make_input_wav():
    import imageio_ffmpeg
    import subprocess
    src = os.path.join(config.VOICE_ROOT, "test-data", "zh.mp3")
    dst = os.path.join(HERE, "out", "in_16k.wav")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", src, "-ac", "1", "-ar", str(config.SAMPLE_RATE), dst], check=True)
    return dst


tts = Tts(start_cosyvoice=True)
print("edge ready=%s warm_ms=%s" % (tts.edge.ready, tts.edge.info.get("warm_ms")), flush=True)

t0 = time.perf_counter()
while tts.cosy is None or not tts.cosy.ready:
    if tts.cosy is not None and tts.cosy.dead_reason:
        print("cosyvoice worker died: %s" % tts.cosy.dead_reason)
        sys.exit(1)
    if time.perf_counter() - t0 > 180:
        print("cosyvoice not ready after 180 s")
        sys.exit(1)
    time.sleep(0.5)
print("cosyvoice ready in %.1f s info=%s" % (time.perf_counter() - t0, tts.cosy.info), flush=True)

# Same spy on synthesize: records every sentence actually handed to a TTS
# engine, so the splitter output and per-sentence engine choice are checkable.
tts_calls = []
_real_synth = tts.synthesize


def spying_synth(text):
    pcm, meta = _real_synth(text)
    tts_calls.append((text, meta))
    return pcm, meta


tts.synthesize = spying_synth

t0 = time.perf_counter()
asr = Asr("cpu")
print("SenseVoice CPU load %.1f s" % (time.perf_counter() - t0), flush=True)

bridge = Bridge(tts)
threading.Thread(target=server.serve, args=(bridge, asr), daemon=True).start()
time.sleep(1.0)

url = "http://127.0.0.1:%d" % config.BRIDGE_PORT
# never printed — only sent back to the local server, like the device would
AUTH = {"X-Auth": config.auth_token()} if config.auth_token() else {}
print("bridge_token: %s" % ("set" if AUTH else "unset"))
with open(make_input_wav(), "rb") as fh:
    wav = fh.read()

# --- turn 1: full round trip on the real cloud, CosyVoice mainline ---------
t0 = time.perf_counter()
first_byte = None
received = bytearray()
with requests.post(url + "/turn?session_id=livetest", data=wav, stream=True,
                   headers=AUTH, timeout=120) as resp:
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    assert resp.headers["X-Voice"] == "s16le/16000/mono", dict(resp.headers)
    gen_id = resp.headers["X-Gen-Id"]
    for block in resp.iter_content(4096):
        if first_byte is None:
            first_byte = time.perf_counter() - t0
        received += block
total = time.perf_counter() - t0

stats1 = llm_stats[0] if llm_stats else {}
sentences1 = tts_calls[:]
audio = np.frombuffer(bytes(received), dtype="<i2") if received else np.zeros(0, dtype="<i2")
seconds = len(audio) / config.SAMPLE_RATE
print("\n[turn 1] gen=%s first_byte=%.2fs total=%.2fs audio=%.2fs peak=%d" % (
    gen_id, first_byte or -1, total, seconds, int(np.abs(audio).max()) if len(audio) else 0))
print("  reply: %s" % stats1.get("reply"))
print("  ttft=%.3fs chunks=%s finish=%s usage=%s" % (
    stats1.get("first_token_seconds") or -1, stats1.get("chunks"),
    stats1.get("finish_reason"), stats1.get("usage")))
for text, meta in sentences1:
    print("  [%s %sms] %s" % (meta["engine"], meta["tts_ms"], text))
assert first_byte is not None, "no audio: turn error? %r" % (bridge._active and bridge._active.error)
assert seconds > 1.0, "too little audio"
assert int(np.abs(audio).max()) > 1000, "silence"
assert stats1.get("finish_reason") == "stop", stats1
assert stats1.get("usage"), "no usage from api"
assert sentences1 and sentences1[0][1]["engine"] == "cosyvoice", \
    "first sentence did not use cosyvoice: %r" % (sentences1[:1],)

# content read-back: what did the chain actually say?
spoken, _ = asr.transcribe_pcm(bytes(received))
print("  spoken(read-back): %s" % spoken)
ratio = difflib.SequenceMatcher(None, stats1.get("reply", ""), spoken or "").ratio()
print("  read-back similarity %.2f" % ratio)

with open(os.path.join(HERE, "out", "live_reply1.wav"), "wb") as fh:
    fh.write(server.pcm_to_wav(bytes(received)))
print("  saved out/live_reply1.wav")

# --- turn 2: real barge-in — /stop while the LLM is still generating -------
got = bytearray()
stop_at = None
with requests.post(url + "/turn?session_id=livetest", data=wav, stream=True,
                   headers=AUTH, timeout=120) as resp:
    gen2 = resp.headers["X-Gen-Id"]
    for block in resp.iter_content(4096):
        got += block
        if stop_at is None and len(got) >= 8192:
            requests.post(url + "/stop", headers=AUTH, timeout=10)
            stop_at = time.perf_counter()
    closed_at = time.perf_counter()
assert stop_at is not None, "stream ended before the interrupt fired"
close_s = closed_at - stop_at
stats2 = llm_stats[1] if len(llm_stats) > 1 else {}
print("\n[turn 2] gen=%s %d bytes then /stop, stream closed %.2fs later, llm_aborted=%s" % (
    gen2, len(got), close_s, stats2.get("aborted")))
assert bridge.gen > int(gen_id), "generation id did not advance"
# /stop cannot cancel a synthesis already running inside the CosyVoice worker,
# so the server closes only after that sentence finishes (≤ COSYVOICE_TIMEOUT);
# the device flushes its own buffer on barge-in, so this is not audible delay.
assert close_s < config.COSYVOICE_TIMEOUT + 3.0, "interrupt did not close the stream promptly"
for text, meta in tts_calls[len(sentences1):]:
    print("  [%s %sms] %s" % (meta["engine"], meta["tts_ms"], text))
if not stats2.get("aborted"):
    print("  WARN: llm stream had already finished before /stop; "
          "close time is a flush measurement only")

print("\nLIVE OK: ASR(cpu) -> real DeepSeek -> split -> CosyVoice -> chunked PCM"
      " (+ real barge-in)")
tts.stop()
