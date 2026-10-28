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

RATE = 16000
BYTES_PER_SEC = RATE * 2
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_SECONDS = 600
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg")


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


def _read_entries():
    """歌单原始条目（不管文件在不在），写回时用它保住顺序。"""
    try:
        with open(PLAYLIST, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    songs = data.get("songs") if isinstance(data, dict) else None
    return [s for s in (songs or []) if isinstance(s, dict) and s.get("file")]


def _write_entries(items):
    SONGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(PLAYLIST) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"songs": items}, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, PLAYLIST)


def catalog():
    """调试台用：列出歌单，包括文件已经丢了的条目。"""
    rows = []
    for item in _read_entries():
        path = SONGS_DIR / item["file"]
        rows.append({"title": item.get("title") or "",
                     "file": item["file"],
                     "seconds": item.get("seconds"),
                     "source": item.get("source"),
                     "missing": not path.is_file()})
    return rows


def _safe_stem(name):
    base = os.path.splitext(os.path.basename(str(name or "")))[0]
    base = unicodedata.normalize("NFKC", base).lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return base or "song"


def _free_name(stem):
    taken = {item["file"] for item in _read_entries()}
    candidate = stem + ".pcm"
    n = 2
    while candidate in taken or (SONGS_DIR / candidate).exists():
        candidate = "%s-%d.pcm" % (stem, n)
        n += 1
    return candidate


def _to_pcm(blob):
    """任意常见音频 → 16 kHz 单声道 s16le。"""
    import io

    import numpy as np
    import soundfile as sf

    try:
        info = sf.info(io.BytesIO(blob))
    except Exception as exc:
        raise ValueError("读不出这个音频：%s" % exc)
    if info.frames <= 0 or info.samplerate <= 0:
        raise ValueError("音频是空的")
    if info.frames / float(info.samplerate) > MAX_SECONDS:
        raise ValueError("超过 %d 分钟，太长了" % (MAX_SECONDS // 60))
    try:
        data, rate = sf.read(io.BytesIO(blob), dtype="float32", always_2d=True)
    except Exception as exc:
        raise ValueError("读不出这个音频：%s" % exc)
    mono = data.mean(axis=1)
    if rate != RATE:
        import soxr
        mono = soxr.resample(mono, rate, RATE)
    if len(mono) == 0:
        raise ValueError("音频是空的")
    peak = float(np.max(np.abs(mono)))
    if peak > 1.0:
        mono = mono / peak
    return (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def add_song(title, name, blob):
    """把上传的音频转成 PCM 存进歌单，返回写进去的条目。"""
    ext = os.path.splitext(str(name or ""))[1].lower()
    if ext not in AUDIO_EXTS:
        raise ValueError("只支持 wav / mp3 / flac / ogg")
    if not blob:
        raise ValueError("文件是空的")
    if len(blob) > MAX_UPLOAD_BYTES:
        raise ValueError("文件超过 %d MB" % (MAX_UPLOAD_BYTES // (1024 * 1024)))
    pcm = _to_pcm(blob)
    title = (title or "").strip() or os.path.splitext(
        os.path.basename(str(name)))[0] or "未命名"
    SONGS_DIR.mkdir(parents=True, exist_ok=True)
    target = _free_name(_safe_stem(name))
    path = SONGS_DIR / target
    with open(path, "wb") as fh:
        fh.write(pcm)
    entry = {"title": title, "aliases": [], "file": target,
             "seconds": round(len(pcm) / float(BYTES_PER_SEC), 3),
             "source": "upload"}
    try:
        _write_entries(_read_entries() + [entry])
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    reset()
    return dict(entry, path=str(path))


def remove_song(file):
    """从歌单和磁盘上同时删掉一首；找不到就 KeyError。"""
    name = os.path.basename(str(file or ""))
    items = _read_entries()
    keep = [item for item in items if item["file"] != name]
    if not name or len(keep) == len(items):
        raise KeyError(file)
    try:
        os.remove(SONGS_DIR / name)
    except OSError:
        pass
    _write_entries(keep)
    reset()
    return {"ok": True, "file": name}


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
