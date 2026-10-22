import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from tts import TtsWorker

t0 = time.perf_counter()
w = TtsWorker("edge", config.SYSTEM_PY, "worker_edge.py", 90)
print("worker up in %.2f s  warm_ms=%s  zh_voices=%d" % (
    time.perf_counter() - t0, w.info.get("warm_ms"), len(w.info.get("zh_voices", []))))
assert w.ready, w.dead_reason

for text in ("好。", "今天天气真不错，我们一起出去玩吧。", "我听到了，你继续说。", "好。"):
    t1 = time.perf_counter()
    pcm, ms, samples = w.request({"text": text, "voice": config.EDGE_VOICE}, 25)
    audio = np.frombuffer(pcm, dtype="<i2")
    seconds = samples / config.SAMPLE_RATE
    print("%2d字 worker=%5dms wall=%5dms audio=%.2fs rtf=%.2f peak=%5d" % (
        len(text), ms, round((time.perf_counter() - t1) * 1000), seconds,
        ms / 1000.0 / seconds if seconds else 0, int(np.abs(audio).max())))
    assert seconds > 0.2 and np.abs(audio).max() > 500, "near-silent output"

w.stop()
print("edge worker OK")
