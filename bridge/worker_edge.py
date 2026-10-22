import asyncio
import base64
import json
import subprocess
import sys
import threading
import time

import edge_tts
import imageio_ffmpeg

OUT_RATE = 16000
VOICE = "zh-CN-XiaoxiaoNeural"

LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


def write(obj):
    # Byte-level UTF-8 on purpose: on Windows an inherited stdout is the ANSI
    # codepage (GBK here), which the parent then fails to decode as UTF-8.
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def decode_mp3(mp3):
    """Hand the 24 kHz mp3 to ffmpeg and get 16 kHz mono s16le straight back."""
    proc = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
         "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1",
         "-ar", str(OUT_RATE), "-"],
        input=mp3, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300])
    return proc.stdout


async def _stream(text, voice):
    # No shared connector on purpose: edge_tts opens a fresh WebSocket per
    # request and ClientSession() closes any connector handed to it.
    buf = bytearray()
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    return bytes(buf)


def submit(coro, timeout=25):
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout)


t0 = time.perf_counter()
zh = sorted({v["ShortName"] for v in submit(edge_tts.list_voices())
             if v["ShortName"].startswith("zh-")})
voices_ms = round((time.perf_counter() - t0) * 1000)
t1 = time.perf_counter()
submit(_stream("好", VOICE))
warm_ms = round((time.perf_counter() - t1) * 1000)

write({"ready": True, "engine": "edge", "voice": VOICE, "zh_voices": zh,
       "voices_ms": voices_ms, "warm_ms": warm_ms})

for raw in sys.stdin.buffer:
    line = raw.strip()
    if not line:
        continue
    try:
        req = json.loads(line.decode("utf-8"))
    except ValueError:
        continue
    rid = req.get("id")
    t2 = time.perf_counter()
    try:
        mp3 = submit(_stream(req["text"], req.get("voice") or VOICE))
        if not mp3:
            raise RuntimeError("empty audio")
        pcm = decode_mp3(mp3)
        write({"id": rid, "ok": True, "ms": round((time.perf_counter() - t2) * 1000),
               "samples": len(pcm) // 2, "b64": base64.b64encode(pcm).decode("ascii")})
    except Exception as exc:
        write({"id": rid, "ok": False, "err": "%s: %s" % (type(exc).__name__, exc)})
