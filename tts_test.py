import sys, os, io, time, json

ROOT = r"C:\Users\DELL\voice-ai\CosyVoice"
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "third_party", "Matcha-TTS"))

import torchaudio
from cosyvoice.cli.cosyvoice import CosyVoice

MODEL_DIR = r"C:\Users\DELL\voice-ai\models\CosyVoice-300M-SFT"
TEXT = "你好，我是本地语音合成模型，现在正在测试真实的合成延迟。"
OUT = r"C:\Users\DELL\voice-ai\test-data\tts_out.wav"

t0 = time.time()
m = CosyVoice(MODEL_DIR, load_jit=False, load_trt=False, fp16=False)
load_s = time.time() - t0

spks = m.list_available_spks()
first_char = None
chunks = []
t1 = time.time()
for out in m.inference_sft(TEXT, spks[0], stream=False, speed=1.0):
    if first_char is None:
        first_char = time.time() - t1
    chunks.append(out["tts_speech"])
total_s = time.time() - t1

import torch
speech = chunks[0] if len(chunks) == 1 else torch.cat(chunks, dim=1)
torchaudio.save(OUT, speech, m.sample_rate)

dur = speech.shape[1] / m.sample_rate
report = {
    "model_load_seconds": round(load_s, 2),
    "synth_seconds": round(total_s, 2),
    "audio_duration_seconds": round(dur, 2),
    "rtf": round(total_s / dur, 3),
    "speakers": spks,
    "speaker_used": spks[0],
    "chars": len(TEXT),
    "seconds_per_char": round(total_s / len(TEXT), 4),
    "out_file": OUT,
}
io.open(r"C:\Users\DELL\voice-ai\tts_report.json", "w", encoding="utf-8").write(
    json.dumps(report, ensure_ascii=False, indent=2))
print("done")
