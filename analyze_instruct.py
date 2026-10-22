import io, json, os, sys
import numpy as np
import librosa

DIR = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\DELL\voice-ai\test-data\instruct"
SR = 22050
TAGS = ["plain_sft", "plain", "sad", "happy", "angry"]

rows = {}
for tag in TAGS:
    p = os.path.join(DIR, tag + ".wav")
    if not os.path.exists(p):
        continue
    y, _ = librosa.load(p, sr=SR)
    f0 = np.asarray(librosa.yin(y, fmin=60, fmax=400, sr=SR), dtype=np.float64)
    valid = np.isfinite(f0) & (f0 > 65) & (f0 < 380)
    f0v = f0[valid]
    rms = librosa.feature.rms(y=y[None, :], frame_length=1024)[0]
    voiced_ratio = float(np.mean(valid))
    rows[tag] = {
        "duration_s": round(len(y) / SR, 3),
        "f0_mean_hz": round(float(np.median(f0v)), 1),
        "f0_std_hz": round(float(np.std(f0v)), 1),
        "f0_p10_hz": round(float(np.percentile(f0v, 10)), 1),
        "f0_p90_hz": round(float(np.percentile(f0v, 90)), 1),
        "rms_mean": round(float(np.mean(rms)), 5),
        "voiced_ratio": round(voiced_ratio, 3),
    }

pairs = []
base = rows.get("plain_sft")
for a in range(len(TAGS)):
    for b in range(a + 1, len(TAGS)):
        ta, tb = TAGS[a], TAGS[b]
        if ta in rows and tb in rows:
            pairs.append({
                "pair": ta + " vs " + tb,
                "f0_mean_delta_hz": round(rows[ta]["f0_mean_hz"] - rows[tb]["f0_mean_hz"], 1),
                "dur_delta_s": round(rows[ta]["duration_s"] - rows[tb]["duration_s"], 3),
                "rms_ratio": round(rows[ta]["rms_mean"] / rows[tb]["rms_mean"], 3) if rows[tb]["rms_mean"] else None,
            })

out = {"per_tag": rows, "pairwise": pairs}
io.open(os.path.join(DIR, "prosody_analysis.json"), "w", encoding="utf-8").write(
    json.dumps(out, ensure_ascii=False, indent=2))
print("done")
