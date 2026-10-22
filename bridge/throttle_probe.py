"""Attribute the sustained-synthesis slowdown seen in test_live turn 2
(210 ms/char in turn 1 -> 459 ms/char in turn 2): GPU thermal/power throttling
vs the resident-SenseVoice memory footprint vs the bridge HTTP layer.

The HTTP layer is excluded entirely — every phase drives the same resident
CosyVoice worker, exactly like cosy_probe.py:

  A  4 rounds x the probe's 4 sentences, back-to-back   GPU sustained, nothing else resident
  B  SenseVoice loaded resident in this process, 3 rounds   replicates the serve-mode footprint
  C  cool the GPU back to its starting temp, 1 round, SenseVoice STILL resident
                                                        fast again => thermal; still slow => memory

A 1 Hz nvidia-smi sampler logs SM clock / temp / power / throttle reasons plus the
compute-app pid/name list, so each sentence can be correlated with the clocks it
actually ran at and a second CUDA process can be named instead of inferred from
total memory afterwards.

    ../.venv/Scripts/python.exe throttle_probe.py
"""
import json
import os
import subprocess
import sys
import threading
import time

import config
from tts import TtsWorker

OUT_DIR = os.path.join(config.VOICE_ROOT, "test-data", "throttle_probe")

SENTENCES = [
    "今天天气不错。",
    "我们一起去吃饭吧。",
    "这件事我先帮你想想，你等我一下。",
    "陪伴这件事最重要的不是说什么，而是什么时候说。",
]

TELEMETRY = []  # (perf_ts, sm_clock, temp_c, power_w, mem_mb, throttle_hex, apps)
_fields = "clocks.sm,temperature.gpu,power.draw,memory.used,clocks_throttle_reasons.active"
_app_fields = "pid,process_name"
_BASELINE_APPS = set()  # idle processes (e.g. the desktop) that are not contention


def _sample_apps():
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=" + _app_fields, "--format=csv,noheader"],
                         capture_output=True, text=True).stdout.strip()
    apps = []
    for line in out.splitlines():
        pid, _, name = line.partition(",")
        pid = pid.strip()
        if pid.isdigit():
            apps.append("%s:%s" % (pid, os.path.basename(name.strip())))
    return "|".join(apps)


def _sample_once():
    out = subprocess.run(["nvidia-smi", "--query-gpu=" + _fields, "--format=csv,noheader"],
                         capture_output=True, text=True).stdout.strip()
    clock, temp, power, mem, thr = [x.strip().split()[0] for x in out.split(",")]
    return (time.perf_counter(), int(clock), int(temp), float(power), int(mem), int(thr, 16),
            _sample_apps())


def _sampler(stop_evt):
    while not stop_evt.is_set():
        try:
            TELEMETRY.append(_sample_once())
        except Exception:
            pass
        stop_evt.wait(1.0)


def _window(t0, t1):
    """Mean SM clock / mean temp / max throttle bits / apps seen across [t0, t1]."""
    xs = [x for x in TELEMETRY if t0 <= x[0] <= t1]
    if not xs:
        return None
    apps = sorted({a for x in xs for a in x[6].split("|") if a})
    return {"clock_mhz": round(sum(x[1] for x in xs) / len(xs)),
            "temp_c": round(sum(x[2] for x in xs) / len(xs), 1),
            "power_w": round(sum(x[3] for x in xs) / len(xs), 1),
            "thr_bits": max(x[5] for x in xs),
            "apps": apps}


def free_ram_gb():
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB"],
        capture_output=True, text=True).stdout.strip()
    try:
        return round(float(out), 2)
    except ValueError:
        return None


THROTTLE_NAMES = {0x4: "SW_POWER_CAP", 0x8: "HW_SLOWDOWN", 0x20: "SW_THERMAL", 0x40: "HW_THERMAL"}


def decode_thr(bits):
    names = [n for bit, n in THROTTLE_NAMES.items() if bits & bit]
    return "+".join(names) if names else "-"


def run_rounds(worker, rounds, label):
    rows = []
    for r in range(rounds):
        for i, text in enumerate(SENTENCES):
            t0 = time.perf_counter()
            pcm, ms, samples = worker.request({"text": text, "spk": config.COSYVOICE_SPEAKER}, 120)
            t1 = time.perf_counter()
            row = {"phase": label, "round": r, "chars": len(text), "synth_ms": ms,
                   "wall_ms": round((t1 - t0) * 1000),
                   "audio_s": round(samples / float(config.SAMPLE_RATE), 2),
                   "ms_per_char": round(ms / float(len(text)), 1),
                   "gpu": _window(t0 - 0.5, t1 + 0.5)}
            rows.append(row)
            gpu = row["gpu"] or {}
            own = "%s:" % worker.pid
            extra = [a for a in gpu.get("apps", []) if a not in _BASELINE_APPS and not a.startswith(own)]
            print("%s r%d s%d %2d字  synth %5d ms  %5.1f ms/字   [%s MHz  %s C  %s W  %s]%s" % (
                label, r, i, row["chars"], ms, row["ms_per_char"],
                gpu.get("clock_mhz", "?"), gpu.get("temp_c", "?"),
                gpu.get("power_w", "?"), decode_thr(gpu.get("thr_bits", 0)),
                "  apps=[" + ", ".join(extra) + "]" if extra else ""), flush=True)
            if worker.dead_reason:
                print("worker died: %s" % worker.dead_reason, flush=True)
                return rows
    return rows


def summarize(rows):
    print("\n==== per-round mean ms/char ====", flush=True)
    by = {}
    for row in rows:
        by.setdefault((row["phase"], row["round"]), []).append(row["ms_per_char"])
    for (phase, rnd), xs in sorted(by.items()):
        print("%s r%d: %5.1f ms/字  (over %d sentences)" % (phase, rnd, sum(xs) / len(xs), len(xs)))
    fast = [r["gpu"] for r in rows if r["ms_per_char"] <= 260 and r["gpu"]]
    slow = [r["gpu"] for r in rows if r["ms_per_char"] > 260 and r["gpu"]]
    for name, group in (("<=260", fast), (">260", slow)):
        if group:
            print("synth %s ms/字: mean clock %4.0f MHz  mean temp %.1f C  mean power %.1f W" % (
                name, sum(g["clock_mhz"] for g in group) / len(group),
                sum(g["temp_c"] for g in group) / len(group),
                sum(g["power_w"] for g in group) / len(group)), flush=True)


def main():
    config.force_utf8_output()
    os.makedirs(OUT_DIR, exist_ok=True)

    base = _sample_once()
    _BASELINE_APPS.update(base[6].split("|"))
    ram0 = free_ram_gb()
    print("baseline: temp %d C  clock %d MHz  power %.1f W  thr %s  free RAM %s GB  apps=%s" % (
        base[2], base[1], base[3], decode_thr(base[5]), ram0, base[6] or "-"), flush=True)

    stop_evt = threading.Event()
    threading.Thread(target=_sampler, args=(stop_evt,), daemon=True).start()

    t0 = time.perf_counter()
    worker = TtsWorker("cosyvoice-throttle", config.PY311, "worker_cosyvoice.py", 300)
    load_s = time.perf_counter() - t0
    if not worker.ready:
        print("FAILED to load: %s" % worker.dead_reason, flush=True)
        sys.exit(1)
    print("worker load %.1f s (incl. self-warmup); free RAM %s GB" % (load_s, free_ram_gb()), flush=True)

    rows = []
    print("\n---- phase A: GPU sustained, nothing else resident ----", flush=True)
    rows += run_rounds(worker, 4, "A")

    print("\n---- phase B: SenseVoice resident (serve-mode footprint) ----", flush=True)
    ram_before_asr = free_ram_gb()
    from asr_engine import Asr
    t1 = time.perf_counter()
    asr = Asr("cpu")
    try:
        asr.transcribe_pcm(b"\x00\x01" * config.SAMPLE_RATE)  # one forward so the footprint is warm
    except Exception as exc:
        print("warmup transcribe failed (non-fatal): %s" % exc, flush=True)
    print("SenseVoice loaded+warmed in %.1f s; free RAM %s -> %s GB" % (
        time.perf_counter() - t1, ram_before_asr, free_ram_gb()), flush=True)
    rows += run_rounds(worker, 3, "B")

    print("\n---- phase C: cool to baseline, SenseVoice still resident ----", flush=True)
    target = base[2] + 2
    t_cool = time.perf_counter()
    cooled = False
    while time.perf_counter() - t_cool < 240:
        if TELEMETRY and TELEMETRY[-1][2] <= target:
            cooled = True
            break
        time.sleep(5)
    now = TELEMETRY[-1] if TELEMETRY else base
    print("%s to %d C in %.0f s (target %d C)" % (
        "cooled" if cooled else "NOT cooled (timeout)", now[2], time.perf_counter() - t_cool, target),
        flush=True)
    rows += run_rounds(worker, 1, "C")

    worker.stop()
    stop_evt.set()
    time.sleep(2)
    print("\nGPU after stop: %d MB   free RAM: %s GB" % (
        _sample_once()[4], free_ram_gb()), flush=True)
    summarize(rows)

    path = os.path.join(OUT_DIR, "throttle_probe.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"baseline": {"temp_c": base[2], "clock_mhz": base[1], "ram_gb": ram0,
                                "apps": sorted(a for a in _BASELINE_APPS if a)},
                   "load_s": round(load_s, 1), "rows": rows, "telemetry": TELEMETRY},
                  fh, ensure_ascii=False)
    print("wrote %s" % path, flush=True)


if __name__ == "__main__":
    main()
