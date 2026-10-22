import sys, io, re, time, json
from funasr import AutoModel

MODEL_DIR = r"C:\Users\DELL\voice-ai\models\SenseVoiceSmall"

def transcribe(path, device="cuda"):
    m = AutoModel(model=MODEL_DIR, device=device, disable_update=True)
    r = m.generate(input=path, cache={}, language="auto", use_itn=True)[0]
    text = r["text"]
    tags = re.match(r"((?:<\|[^|]*\|>)+)", text)
    lang = emo = ""
    if tags:
        body = text[tags.end():]
        t = re.findall(r"<\|([^|]*)\|>", tags.group(1))
        lang, emo = (t + ["", ""])[:2]
        return body.strip(), lang, emo, float(r.get("rtf_avg", 0))
    return text.strip(), "", "", 0.0

if __name__ == "__main__":
    path = sys.argv[1]
    dev = sys.argv[2] if len(sys.argv) > 2 else "cuda"
    t0 = time.time()
    body, lang, emo, _ = transcribe(path, dev)
    dt = time.time() - t0
    io.open(r"C:\Users\DELL\voice-ai\asr_last.txt", "w", encoding="utf-8").write(body)
    print(json.dumps({"seconds_total": round(dt, 3), "language": lang,
                      "emotion": emo, "out_file": "asr_last.txt"}, ensure_ascii=False))
