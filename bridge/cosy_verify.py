"""Read the CosyVoice probe output back through our own ASR.

Confirms what was actually spoken (not just that synthesis returned bytes).
Run with the .venv interpreter after cosy_probe.py has finished.
"""

import glob
import io
import os
import re
import time
import wave

import config
from asr_engine import Asr, _TAG

OUT_DIR = os.path.join(config.VOICE_ROOT, "test-data", "cosy_probe")


def main():
    config.force_utf8_output()
    paths = sorted(glob.glob(os.path.join(OUT_DIR, "s*.wav")))
    if not paths:
        print("no s*.wav found — run cosy_probe.py first")
        return
    t0 = time.perf_counter()
    asr = Asr(device="cpu")
    print("SenseVoice (CPU) loaded in %.1f s" % (time.perf_counter() - t0), flush=True)
    for path in paths:
        with open(path, "rb") as fh:
            blob = fh.read()
        with wave.open(io.BytesIO(blob), "rb") as wav:
            frames, rate, n = wav.readframes(wav.getnframes()), wav.getframerate(), wav.getnframes()
        t1 = time.perf_counter()
        raw = asr.model.generate(input=path, cache={}, language="auto", use_itn=True)[0]["text"]
        secs = round(time.perf_counter() - t1, 3)
        tag = _TAG.match(raw)
        print("%-8s %5.2f s  asr %5.3f s  %s" %
              (os.path.basename(path), n / float(rate), secs, raw), flush=True)
        print("         -> %s" % (raw[tag.end():].strip() if tag else raw.strip()), flush=True)


if __name__ == "__main__":
    main()
