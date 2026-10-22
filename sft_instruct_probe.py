import sys, os, io, time, json, traceback

ROOT = r"C:\Users\DELL\voice-ai\CosyVoice"
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "third_party", "Matcha-TTS"))

import torch
import torchaudio
import numpy as np
from cosyvoice.cli.cosyvoice import CosyVoice

MODEL_DIR = r"C:\Users\DELL\voice-ai\models\CosyVoice-300M-SFT"
TEXT = "今天天气真不错，我们一起出去玩吧。"
INSTRUCTS = {
    "sad": "用悲伤的语气说这句话。",
    "happy": "用开心兴奋的语气说这句话。",
}
OUT = r"C:\Users\DELL\voice-ai\test-data\instruct"
os.makedirs(OUT, exist_ok=True)


def run(fn, tag):
    t0 = time.time()
    parts = []
    for out in fn():
        parts.append(out["tts_speech"])
    wall = time.time() - t0
    speech = parts[0] if len(parts) == 1 else torch.cat(parts, dim=1)
    path = os.path.join(OUT, "sft_" + tag + ".wav")
    torchaudio.save(path, speech, m.sample_rate)
    wav = speech.numpy().astype(np.float64).ravel()
    return {"tag": tag, "wall_seconds": round(wall, 3),
            "duration_seconds": round(len(wav) / m.sample_rate, 3),
            "rms": round(float(np.sqrt(np.mean(wav ** 2))), 5), "file": path}


t0 = time.time()
m = CosyVoice(MODEL_DIR, load_jit=False, load_trt=False, fp16=False)
load_s = time.time() - t0
spks = m.list_available_spks()

results, errors = [], []
results.append(run(lambda: m.inference_sft(TEXT, spks[0], stream=False), "baseline_sft"))
for tag, ins in INSTRUCTS.items():
    try:
        r = run(lambda: m.inference_instruct(TEXT, spks[0], ins, stream=False), "instruct_" + tag)
        r["instruct"] = ins
        results.append(r)
    except Exception as e:
        errors.append({"instruct": ins, "error": "%s: %s" % (type(e).__name__, str(e)[:300]),
                       "trace": traceback.format_exc().splitlines()[-3:]})

report = {"model": "CosyVoice-300M-SFT probing inference_instruct",
          "model_load_seconds": round(load_s, 2), "available_speakers": spks,
          "text": TEXT, "runs": results, "errors": errors}
io.open(os.path.join(OUT, "sft_instruct_probe.json"), "w", encoding="utf-8").write(
    json.dumps(report, ensure_ascii=False, indent=2))
print("done")
