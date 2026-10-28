# -*- coding: utf-8 -*-
"""把桌宠包的 webm 转成 320x180 JPEG 帧序列，供推流用。

只做构建期转换：运行时 bridge 不依赖 ffmpeg（Windows 上没有）。
用 WSL 的 ffmpeg 6.1.1（本机实测可用），转完直接落在 bridge/screen_frames/。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "screen_frames")
SCRIPT = "/mnt/c/Users/DELL/voice-ai/bridge/tools/convert_frames.sh"
FPS = 10


def names():
    with open(os.path.join(HERE, "frame_names.txt"), encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def convert_all():
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    subprocess.run(["wsl", "-d", "Ubuntu-24.04", "--", "bash", SCRIPT],
                   check=True, env=env)


def write_index():
    index = {}
    for name in names():
        d = os.path.join(OUT, name)
        if not os.path.isdir(d):
            raise SystemExit("missing frames: %s" % name)
        n = len([f for f in os.listdir(d) if f.endswith(".jpg")])
        index[name] = {"frames": n, "fps": FPS}
    tmp = os.path.join(OUT, "index.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, os.path.join(OUT, "index.json"))
    return index


def main():
    convert_all()
    index = write_index()
    for name, meta in sorted(index.items(), key=lambda kv: kv[1]["frames"]):
        print("%-4d %s" % (meta["frames"], name))
    print("total %d anims, %d frames" % (
        len(index), sum(m["frames"] for m in index.values())))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
