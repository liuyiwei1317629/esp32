import base64
import contextlib
import json
import os
import sys
import time

# py311 is an embeddable build: its ._pth file leaves the script directory out
# of sys.path, so our own modules have to be added by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import winperf

# Without this the process can be scheduled onto E-cores and synthesis runs at
# RTF ~1.8 instead of ~0.8, which is slower than real time.
winperf.unthrottle()

COSYVOICE_REPO = r"C:\Users\DELL\voice-ai\CosyVoice"
MODEL_DIR = r"C:\Users\DELL\voice-ai\models\CosyVoice-300M-SFT"
OUT_RATE = 16000

sys.path.insert(0, COSYVOICE_REPO)
sys.path.insert(0, COSYVOICE_REPO + r"\third_party\Matcha-TTS")

import torch
import torchaudio

from cosyvoice.cli.cosyvoice import CosyVoice


def write(obj):
    # Byte-level UTF-8 on purpose: on Windows an inherited stdout is the ANSI
    # codepage (GBK here), which the parent then fails to decode as UTF-8.
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def to_pcm(tensor, rate):
    if tensor.dim() > 1:
        tensor = tensor[0]
    tensor = tensor.float().clamp(-1.0, 1.0)
    if rate != OUT_RATE:
        tensor = torchaudio.functional.resample(tensor, rate, OUT_RATE)
    return (tensor * 32767.0).to(torch.int16).cpu().numpy().tobytes()


# CosyVoice prints model internals to stdout; keep that on stderr so our
# stdout stays a clean JSON-lines channel.
with contextlib.redirect_stdout(sys.stderr):
    model = CosyVoice(MODEL_DIR, load_jit=False, load_trt=False, fp16=config.COSYVOICE_FP16)
    speakers = model.list_available_spks()
    # Measured: the first synthesis after load costs 2694 ms where the same
    # sentence later costs 1441 ms. Pay that here, not in the user's first turn.
    for _ in model.inference_sft("好。", speakers[0], stream=False):
        pass

write({"ready": True, "engine": "cosyvoice", "speakers": speakers,
       "source_rate": model.sample_rate, "fp16": config.COSYVOICE_FP16})

for raw in sys.stdin.buffer:
    line = raw.strip()
    if not line:
        continue
    try:
        req = json.loads(line.decode("utf-8"))
    except ValueError:
        continue
    rid = req.get("id")
    t0 = time.perf_counter()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            chunks = [c["tts_speech"] for c in model.inference_sft(
                req["text"], req.get("spk") or speakers[0],
                stream=False, speed=req.get("speed", 1.0))]
        pcm = to_pcm(torch.cat(chunks, dim=-1) if len(chunks) > 1 else chunks[0], model.sample_rate)
        write({"id": rid, "ok": True, "ms": round((time.perf_counter() - t0) * 1000),
               "samples": len(pcm) // 2, "b64": base64.b64encode(pcm).decode("ascii")})
    except Exception as exc:
        write({"id": rid, "ok": False, "err": "%s: %s" % (type(exc).__name__, exc)})
