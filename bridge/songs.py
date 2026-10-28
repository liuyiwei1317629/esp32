"""原唱点播：把一句话映射到本机 PCM 文件。

只做匹配和轮转状态，不碰网络；server 负责把 PCM 写进 /turn 的回复流。
音频固定 16 kHz 单声道 s16le——和固件 X-Voice 的要求一致。
"""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from pathlib import Path

SONGS_DIR = Path(os.environ.get("SONGS_DIR")
                 or Path(__file__).resolve().parent.parent / "songs")
PLAYLIST = SONGS_DIR / "playlist.json"

_VERBS = ("放", "唱", "听", "来", "播放")
_NEXT_PHRASES = ("下一首", "换一首", "再来一首", "换一个", "再放一首")
_REPEAT_PHRASES = ("再放一遍", "再唱一遍", "重复一遍", "再来一遍")
_GENERIC_PHRASES = ("放首歌", "放一首歌", "唱首歌", "唱一首歌",
                    "来一首", "来首歌", "放音乐", "放歌")

_lock = threading.Lock()
_state = {"next": 0, "last": None}


def _norm(text):
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", text or "").lower())


def load():
    try:
        with open(PLAYLIST, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for item in data.get("songs") or []:
        if not isinstance(item, dict) or not item.get("file"):
            continue
        path = SONGS_DIR / item["file"]
        if not path.is_file():
            continue
        out.append(dict(item, path=str(path)))
    return out


def reset():
    with _lock:
        _state["next"] = 0
        _state["last"] = None


def _play(index, songs):
    with _lock:
        _state["last"] = index
        _state["next"] = (index + 1) % len(songs)
    return songs[index]


def _play_next(songs):
    with _lock:
        index = _state["next"] % len(songs)
        _state["last"] = index
        _state["next"] = (index + 1) % len(songs)
    return songs[index]


def _play_repeat(songs):
    with _lock:
        index = _state["last"]
        if index is None:
            index = 0
            _state["next"] = 1 % len(songs)
        _state["last"] = index
    return songs[index]


def pick(text):
    """命中点播就返回歌曲条目（含 path），否则 None。"""
    songs = load()
    if not songs:
        return None
    n = _norm(text)
    if not n:
        return None
    has_verb = any(v in n for v in _VERBS)
    for index, song in enumerate(songs):
        keys = [song.get("title") or ""] + list(song.get("aliases") or [])
        for key in keys:
            k = _norm(key)
            if k and (n == k or (has_verb and k in n)):
                return _play(index, songs)
    if any(_norm(p) in n for p in _REPEAT_PHRASES):
        return _play_repeat(songs)
    if any(_norm(p) in n for p in _NEXT_PHRASES):
        return _play_next(songs)
    if n in ("唱歌", "听歌"):
        return _play_next(songs)
    if any(_norm(p) in n for p in _GENERIC_PHRASES):
        return _play_next(songs)
    return None


def prompt_block():
    """给大模型的本机点播说明；没有可用歌单时返回空串。"""
    songs = load()
    if not songs:
        return ""
    titles = "、".join("《%s》" % (s.get("title") or "") for s in songs)
    return ("本机点播：你可以播放这几首原唱：%s。用户说“放《歌名》”就直接播放；"
            "被问到能唱什么、能放什么时只列这几首，并说明放的是原唱，"
            "不是你本人翻唱，也不要承诺会唱别的歌。" % titles)


def augment_messages(messages):
    """把点播说明追加到 system 消息；结构不对就原样返回。"""
    block = prompt_block()
    if not block or not isinstance(messages, list) or not messages:
        return messages
    first = messages[0]
    if not isinstance(first, dict):
        return messages
    if first.get("role") == "system":
        first["content"] = (str(first.get("content") or "").rstrip()
                            + "\n\n" + block)
        return messages
    messages.insert(0, {"role": "system", "content": block})
    return messages
