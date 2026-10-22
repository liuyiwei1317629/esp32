import sys, time, json
from funasr import AutoModel

MODEL_DIR = r"C:\Users\DELL\voice-ai\models\SenseVoiceSmall"

m = AutoModel(model=MODEL_DIR, device="cuda", disable_update=True)

audio = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\DELL\voice-ai\test-data\zh.mp3"

for tag in ("GPU首次(含加载)", "GPU第二次"):
    t0 = time.time()
    r = m.generate(input=audio, cache={}, language="auto", use_itn=True,
                   ban_emo_unk=False)
    dt = time.time() - t0
    out = r[0]
    text = out.get("text", "")
    if tag == "GPU首次(含加载)":
        first = (tag, dt, text)
    else:
        print(json.dumps({"phase": tag, "seconds": round(dt, 3), "text": text}, ensure_ascii=False))
print(json.dumps({"phase": first[0], "seconds": round(first[1], 3), "text": first[2]}, ensure_ascii=False), file=sys.stderr)
