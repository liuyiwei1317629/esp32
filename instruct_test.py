import sys, os, io, time, json

ROOT = r"C:\Users\DELL\voice-ai\CosyVoice"
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "third_party", "Matcha-TTS"))

import torch
import torchaudio
import numpy as np
from cosyvoice.cli.cosyvoice import CosyVoice

MODEL_DIR = r"C:\Users\DELL\voice-ai\models\CosyVoice-300M-Instruct"
TEXT = "今天天气真不错，我们一起出去玩吧。"
OUT = r"C:\Users\DELL\voice-ai\test-data\instruct"
os.makedirs(OUT, exist_ok=True)

INSTRUCTS = {
    "sad": "用悲伤的语气说这句话。",
    "happy": "用开心兴奋的语气说这句话。",
    "angry": "用愤怒强硬语气说这句话。",
    "plain_sft": None,
}


def synth(fn, tag):
    t0 = time.time()
    first = None
    parts = []
    for out in fn():
        if first is None:
            first = time.time() - t0
        parts.append(out["tts_speech"])
    wall = time.time() - t0
    speech = parts[0] if len(parts) == 1 else torch.cat(parts, dim=1)
    path = os.path.join(OUT, tag + ".wav")
    torchaudio.save(path, speech, m.sample_rate)
    wav = speech.numpy().astype(np.float64).ravel()
    dur = len(wav) / m.sample_rate
    return {
        "tag": tag,
        "wall_seconds": round(wall, 3),
        "first_chunk_seconds": round(first, 3),
        "duration_seconds": round(dur, 3),
        "rms": round(float(np.sqrt(np.mean(wav ** 2))), 5),
        "peak": round(float(np.max(np.abs(wav))), 4),
        "chunks": len(parts),
        "file": path,
    }


t0 = time.time()
m = CosyVoice(MODEL_DIR, load_jit=False, load_trt=False, fp16=False)
load_s = time.time() - t0

spks = m.list_available_spks()
results = []
for tag, ins in INSTRUCTS.items():
    if ins is None:
        r = synth(lambda: m.inference_sft(TEXT, spks[0], stream=True, speed=1.0), tag)
        r["instruct"] = "(none, inference_sft)"
    else:
        r = synth(lambda: m.inference_instruct(TEXT, spks[0], ins, stream=True, speed=1.0), tag)
        r["instruct"] = ins
    results.append(r)

report = {
    "model": "CosyVoice-300M-Instruct",
    "model_load_seconds": round(load_s, 2),
    "available_speakers": spks,
    "speaker_used": spks[0],
    "text": TEXT,
    "runs": results,
}
io.open(os.path.join(OUT, "instruct_report.json"), "w", encoding="utf-8").write(
    json.dumps(report, ensure_ascii=False, indent=2))
print("done")
