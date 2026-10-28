import base64
import contextlib
import json
import os
import sys
import time
from pathlib import Path

# This worker runs inside WSL (Linux paths, CUDA build of torch). The bridge
# launches it through wsl.exe and speaks the same JSON-lines protocol as the
# other workers, so only the command line differs.
MODEL_DIR = Path(os.environ.get("FEIYU_MODEL_DIR", "/root/feiyu-voice/model"))
STYLE = os.environ.get("FEIYU_STYLE", "cute")
OUT_RATE = 16000

manifest = json.loads((MODEL_DIR / "manifest.json").read_text(encoding="utf-8-sig"))
runtime = json.loads((MODEL_DIR / "runtime.local.json").read_text(encoding="utf-8-sig"))
selected = manifest["styles"][STYLE]
params = manifest["inference"]
repo = Path(runtime["repo"])

os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                  PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="4")
os.chdir(repo)
sys.path.insert(0, str(repo))
sys.path.append(str(repo / "third_party/Matcha-TTS"))

import torch
import torchaudio
import numpy as np

from cosyvoice.cli.cosyvoice import AutoModel
from cosyvoice.utils.common import set_all_random_seed

# CosyVoice3 pads the front of a sentence with up to ~1.8 s of near-digital
# silence (measured 0.10-1.82 s over four sentences, RMS 2e-5). The device
# plays that as dead air before every sentence, so it is trimmed here. Real
# speech in these samples never dips below 0.01 peak, so 0.009 separates them.
LEAD_THRESHOLD = 0.009
LEAD_PREROLL_S = 0.06


def write(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def to_pcm(audio, rate):
    if audio.dim() > 1:
        audio = audio[0]
    audio = audio.float()
    if rate != OUT_RATE:
        audio = torchaudio.functional.resample(audio, rate, OUT_RATE)
    audio = audio.clamp(-1.0, 1.0)
    pcm = (audio * 32767.0).to(torch.int16).cpu().numpy()
    loud = np.abs(pcm) > int(LEAD_THRESHOLD * 32767)
    if loud.any():
        start = max(0, int(np.argmax(loud)) - int(OUT_RATE * LEAD_PREROLL_S))
        pcm = pcm[start:]
    return pcm.tobytes()


# CosyVoice prints progress bars and logs to stdout; keep our stdout a clean
# JSON-lines channel.
with contextlib.redirect_stdout(sys.stderr):
    t0 = time.perf_counter()
    torch.set_num_threads(4)
    model = AutoModel(model_dir=str(MODEL_DIR / manifest["base_model_dir"]),
                      fp16=False, load_trt=False, load_vllm=False)
    checkpoint = MODEL_DIR / selected["llm_checkpoint"]
    if checkpoint != MODEL_DIR / manifest["base_model_dir"] / "llm.pt":
        state = torch.load(checkpoint, map_location="cpu", weights_only=True)
        model.model.llm.load_state_dict(state, strict=True)
        del state
    model.model.llm.eval()
    set_all_random_seed(params["seed"])
    prompt_text = params["prompt_prefix"] + selected["reference_text"]
    reference = str(MODEL_DIR / selected["reference_audio"])
    # The reference audio's speech feat/token/spk embedding cost 1.7-2.5 s on
    # every call; register them once under an id and reuse it per sentence.
    spk_id = "feiyu_" + STYLE
    model.add_zero_shot_spk(prompt_text, reference, spk_id)
    # First synth after load costs several times a warm one (measured 32 s vs
    # 3-5 s), so pay it before the bridge starts sending sentences. The text
    # must be sentence-length: a two-character warm-up crashes the F0 predictor
    # ("kernel size can't be greater than actual input size").
    for _ in model.inference_zero_shot("我在呢，你想说什么就慢慢说，我听着。", prompt_text, reference,
                                       zero_shot_spk_id=spk_id,
                                       stream=False, speed=1.0, text_frontend=True):
        pass
    load_s = time.perf_counter() - t0

write({"ready": True, "engine": "feiyu", "style": STYLE,
       "candidate": selected["candidate"], "source_rate": model.sample_rate,
       "load_s": round(load_s, 1)})

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
            chunks = [c["tts_speech"] for c in model.inference_zero_shot(
                req["text"], prompt_text, reference, zero_shot_spk_id=spk_id,
                stream=False, speed=req.get("speed", 1.0), text_frontend=True)]
        audio = torch.cat(chunks, dim=-1) if len(chunks) > 1 else chunks[0]
        pcm = to_pcm(audio, model.sample_rate)
        write({"id": rid, "ok": True, "ms": round((time.perf_counter() - t0) * 1000),
               "samples": len(pcm) // 2, "b64": base64.b64encode(pcm).decode("ascii")})
    except Exception as exc:
        write({"id": rid, "ok": False, "err": "%s: %s" % (type(exc).__name__, exc)})
