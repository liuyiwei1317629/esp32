import sys, os, io, time, json

ROOT = r"C:\Users\DELL\voice-ai\CosyVoice"
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "third_party", "Matcha-TTS"))

import torch
import torchaudio
from cosyvoice.cli.cosyvoice import CosyVoice

MODEL_DIR = r"C:\Users\DELL\voice-ai\models\CosyVoice-300M-SFT"
TEXT = "你好，我是本地语音合成模型，现在正在测试真实的合成延迟。"
OUT_DIR = r"C:\Users\DELL\voice-ai\test-data"
REPORT = os.path.join(OUT_DIR, "tts_bench.json")

FP16 = "--fp16" in sys.argv
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

def once(m, stream, spk):
    t0 = time.time()
    first = None
    parts = []
    for out in m.inference_sft(TEXT, spk, stream=stream, speed=1.0):
        if first is None:
            first = time.time() - t0
        parts.append(out["tts_speech"])
    wall = time.time() - t0
    speech = parts[0] if len(parts) == 1 else torch.cat(parts, dim=1)
    dur = speech.shape[1] / m.sample_rate
    path = os.path.join(OUT_DIR, "tts_%s.wav" % ("stream" if stream else "whole"))
    torchaudio.save(path, speech, m.sample_rate)
    return {
        "mode": "stream" if stream else "whole",
        "chunks": len(parts),
        "wall_seconds": round(wall, 3),
        "first_chunk_seconds": round(first, 3),
        "audio_duration_seconds": round(dur, 3),
        "rtf_wall": round(wall / dur, 3),
        "rtf_first_chunk": round(first / dur, 3),
        "out_file": path,
    }

t0 = time.time()
m = CosyVoice(MODEL_DIR, load_jit=False, load_trt=False, fp16=FP16)
load_s = time.time() - t0

spk = m.list_available_spks()[0]
runs = [once(m, False, spk), once(m, True, spk), once(m, True, spk)]

report = {
    "device": DEVICE,
    "fp16": FP16,
    "model_load_seconds": round(load_s, 2),
    "speaker": spk,
    "chars": len(TEXT),
    "runs": runs,
    "note": "run 2/3 are the warm-process stream case; run 1 is whole-sentence",
}
io.open(REPORT, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
print("done")
