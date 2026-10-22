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
OUT = r"C:\Users\DELL\voice-ai\test-data\instruct2"
os.makedirs(OUT, exist_ok=True)

# 官方格式：前缀 + 中文指令 + <|endofprompt|> 结束标记（见 example.py:86、llm.py:479）
INSTRUCTS = {
    "sad":   "You are a helpful assistant. 请用悲伤低落的语气说这句话。<|endofprompt|>",
    "happy": "You are a helpful assistant. 请用开心兴奋的语气说这句话。<|endofprompt|>",
    "angry": "You are a helpful assistant. 请用愤怒强硬的语气说这句话。<|endofprompt|>",
}

t0 = time.time()
m = CosyVoice(MODEL_DIR, load_jit=False, load_trt=False, fp16=False)
load_s = time.time() - t0
spks = m.list_available_spks()
spk = spks[0]


def run(fn, tag, ins):
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
    return {"tag": tag, "instruct": ins, "wall_seconds": round(wall, 3),
            "first_chunk_seconds": round(first, 3), "duration_seconds": round(dur, 3),
            "rtf": round(wall / dur, 3), "rms": round(float(np.sqrt(np.mean(wav ** 2))), 5),
            "chunks": len(parts), "file": path}


results = [run(lambda: m.inference_sft(TEXT, spk, stream=True, speed=1.0), "plain", "(no instruct)")]
for tag, ins in INSTRUCTS.items():
    try:
        results.append(run(lambda: m.inference_instruct(TEXT, spk, ins, stream=True, speed=1.0), tag, ins))
    except Exception as e:
        results.append({"tag": tag, "instruct": ins, "error": "%s: %s" % (type(e).__name__, str(e)[:200])})

report = {"model": "CosyVoice-300M-Instruct", "format": "official <|endofprompt|>",
          "model_load_seconds": round(load_s, 2), "speaker": spk,
          "available_speakers": spks, "text": TEXT, "runs": results}
io.open(os.path.join(OUT, "report.json"), "w", encoding="utf-8").write(
    json.dumps(report, ensure_ascii=False, indent=2))
print("done")
