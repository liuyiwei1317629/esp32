"""Measure the real first-sentence cost of the CosyVoice main path.

Run with the .venv interpreter:
    ../.venv/Scripts/python.exe cosy_probe.py
"""

import json
import os
import subprocess
import sys
import time
import wave

import config
from tts import TtsWorker

OUT_DIR = os.path.join(config.VOICE_ROOT, "test-data", "cosy_probe")

SENTENCES = [
    "今天天气不错。",
    "我们一起去吃饭吧。",
    "这件事我先帮你想想，你等我一下。",
    "陪伴这件事最重要的不是说什么，而是什么时候说。",
]


def free_ram_gb():
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB"],
        capture_output=True, text=True).stdout.strip()
    try:
        return round(float(out), 2)
    except ValueError:
        return None


def gpu_used_mb():
    """Per-process memory is reported as [N/A] here, so take the GPU total."""
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True).stdout.strip()
    try:
        return int(float(out.splitlines()[0]))
    except (IndexError, ValueError):
        return -1


def save_wav(pcm, path):
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(config.SAMPLE_RATE)
        fh.writeframes(pcm)


def peak(pcm):
    m = 0
    for i in range(0, len(pcm) - 1, 2):
        v = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        m = max(m, abs(v))
    return m


def main():
    config.force_utf8_output()
    os.makedirs(OUT_DIR, exist_ok=True)
    ram_before = free_ram_gb()
    print("free RAM before: %s GB" % ram_before, flush=True)

    t0 = time.perf_counter()
    worker = TtsWorker("cosyvoice-probe", config.PY311, "worker_cosyvoice.py", 300)
    load_s = round(time.perf_counter() - t0, 1)
    if not worker.ready:
        print("FAILED to load: %s" % worker.dead_reason, flush=True)
        sys.exit(1)
    print("cold load %.1f s  speakers=%s  source_rate=%d" %
          (load_s, worker.info.get("speakers"), worker.info.get("source_rate")), flush=True)
    ram_loaded = free_ram_gb()
    print("GPU total used: %d MB   free RAM: %s GB" % (gpu_used_mb(), ram_loaded), flush=True)

    results = []
    schedule = SENTENCES + [SENTENCES[0]]  # the repeat separates first-call cost from steady state
    for i, text in enumerate(schedule):
        t1 = time.perf_counter()
        pcm, ms, samples = worker.request({"text": text, "spk": config.COSYVOICE_SPEAKER}, 120)
        wall = round((time.perf_counter() - t1) * 1000)
        dur = samples / float(config.SAMPLE_RATE)
        path = os.path.join(OUT_DIR, "s%d.wav" % i)
        save_wav(pcm, path)
        row = {"chars": len(text), "text": text, "repeat": i >= len(SENTENCES),
               "synth_ms": ms, "wall_ms": wall,
               "audio_s": round(dur, 2), "rtf": round(ms / 1000.0 / dur, 3),
               "ms_per_char": round(ms / float(len(text)), 1),
               "peak": peak(pcm), "wav": path}
        results.append(row)
        print("s%d%s %2d字  synth %5d ms  audio %4.2f s  RTF %5.2f  %5.1f ms/字  peak %5d" %
              (i, " (repeat)" if row["repeat"] else "        ", row["chars"], ms, dur,
               row["rtf"], row["ms_per_char"], row["peak"]), flush=True)

    worker.stop()
    time.sleep(3)
    print("GPU after stop: %d MB   free RAM: %s GB" % (gpu_used_mb(), free_ram_gb()), flush=True)

    with open(os.path.join(OUT_DIR, "cosy_probe.json"), "w", encoding="utf-8") as fh:
        json.dump({"cold_load_s": load_s, "ram_before_gb": ram_before,
                   "ram_loaded_gb": ram_loaded, "ram_after_stop_gb": free_ram_gb(),
                   "gpu_loaded_mb": gpu_used_mb(), "results": results},
                  fh, ensure_ascii=False, indent=2)
    print("wrote %s" % os.path.join(OUT_DIR, "cosy_probe.json"), flush=True)


if __name__ == "__main__":
    main()
