import os, io, re, sys, json, time
from funasr import AutoModel

MODEL_DIR = r"C:\Users\DELL\voice-ai\models\SenseVoiceSmall"
DIR = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\DELL\voice-ai\test-data\instruct"
FILES = sys.argv[2:] if len(sys.argv) > 2 else ["plain_sft.wav", "plain.wav", "sad.wav", "happy.wav", "angry.wav"]
EXPECT = "今天天气真不错我们一起出去玩吧"

m = AutoModel(model=MODEL_DIR, device="cuda", disable_update=True)

rows = []
for f in FILES:
    p = os.path.join(DIR, f)
    if not os.path.exists(p):
        continue
    t0 = time.time()
    r = m.generate(input=p, cache={}, language="auto", use_itn=True)[0]
    text = r["text"]
    tags = re.match(r"((?:<\|[^|]*\|>)+)", text)
    tag_list = re.findall(r"<\|([^|]*)\|>", tags.group(1)) if tags else []
    body = text[tags.end():].strip() if tags else text.strip()
    norm = re.sub(r"[，。！？、,.!?\s]", "", body)
    rows.append({
        "file": f,
        "transcript": body,
        "tags": tag_list[:3],
        "chars": len(norm),
        "matches_expected": norm == EXPECT,
        "extra_text": norm.replace(EXPECT, "") if EXPECT in norm else "(expected phrase not found)",
        "asr_seconds": round(time.time() - t0, 3),
    })

io.open(os.path.join(DIR, "asr_transcripts.json"), "w", encoding="utf-8").write(
    json.dumps(rows, ensure_ascii=False, indent=2))
print("done")
