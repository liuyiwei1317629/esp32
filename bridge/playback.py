"""放音音量：bridge 端对 16k PCM 做衰减，固件增益不动。

档位存 ~/.voice-ai/playback.json；只允许调小，100 = 原样（1.0x）。
"""

import json
import os

import numpy as np

LEVELS = (30, 50, 70, 100)
DEFAULT_LEVEL = 100


def _path():
    return os.environ.get("PLAYBACK_FILE") or os.path.join(
        os.path.expanduser("~"), ".voice-ai", "playback.json")


def level():
    try:
        with open(_path(), encoding="utf-8") as fh:
            value = int(json.load(fh).get("level"))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_LEVEL
    return value if value in LEVELS else DEFAULT_LEVEL


def set_level(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("level must be one of %s" % (LEVELS,))
    if value not in LEVELS:
        raise ValueError("level must be one of %s" % (LEVELS,))
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"level": value}, fh)
    os.replace(tmp, path)
    return value


def gain():
    return level() / 100.0


def apply(pcm):
    """pcm: s16le 字节。返回衰减后的字节；1.0x 时原样返回。"""
    g = gain()
    if g >= 1.0 or not pcm:
        return pcm
    count = len(pcm) // 2
    if count == 0:
        return pcm
    samples = np.frombuffer(pcm[:count * 2], dtype="<i2").astype(np.float32) * g
    scaled = np.clip(samples, -32768, 32767).astype("<i2").tobytes()
    return scaled + pcm[count * 2:]
