"""Content check on the edge-tts path: does the audio say what we sent?

The earlier e2e run only proved audio came back non-silent. This reads it
through SenseVoice so a mangled-text bug cannot hide behind a valid waveform.
"""

import os
import re
import sys
import time

import config
from asr_engine import Asr
from tts import TtsWorker

TEXT = "今天天气不错，我们去吃饭吧。"
OUT = os.path.join(config.VOICE_ROOT, "test-data", "edge_content.wav")


def norm(s):
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE)


def main():
    config.force_utf8_output()
    t0 = time.perf_counter()
    worker = TtsWorker("edge", config.SYSTEM_PY, "worker_edge.py", 45)
    if not worker.ready:
        print("FAILED edge worker: %s" % worker.dead_reason)
        sys.exit(1)
    pcm, ms, samples = worker.request({"text": TEXT, "voice": config.EDGE_VOICE}, 20)
    worker.stop()
    print("edge: synth %d ms, %.2f s audio" % (ms, samples / float(config.SAMPLE_RATE)), flush=True)

    import wave
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with wave.open(OUT, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(config.SAMPLE_RATE)
        fh.writeframes(pcm)

    t1 = time.perf_counter()
    asr = Asr("cpu")
    print("SenseVoice CPU load %.1f s" % (time.perf_counter() - t1), flush=True)
    heard, secs = asr.transcribe_pcm(pcm)
    print("sent   : %s" % TEXT, flush=True)
    print("heard  : %s  (%.3f s)" % (heard, secs), flush=True)
    if norm(heard) != norm(TEXT):
        print("MISMATCH — the text reaching the synthesizer is not what was spoken")
        sys.exit(1)
    print("edge content OK (round trip %.1f s)" % (time.perf_counter() - t0))


if __name__ == "__main__":
    main()
