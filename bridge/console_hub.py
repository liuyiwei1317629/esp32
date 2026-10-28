"""调试台数据中枢：轮次快照、增量游标、jsonl 落盘。

设计原则：与 `/turn` 契约完全解耦。
这里读到的都是 `report()` 已经算好的数，不额外打点、不改设备侧行为；
所有对外入口都包了 try —— 调试台出问题绝不能连累一轮真实对话。

落盘写在 `~/.voice-ai/console/turns.jsonl`（个人数据目录，不进仓库）。
"""

import collections
import json
import os
import threading
import time

MAX_TURNS = int(os.environ.get("CONSOLE_MAX_TURNS", "200"))
MAX_LOG_LINES = int(os.environ.get("CONSOLE_MAX_LOG_LINES", "800"))
# 32 bytes per millisecond: s16le / 16 kHz / mono.
BYTES_PER_MS = 32


def record_enabled():
    return os.environ.get("CONSOLE_RECORD", "1") != "0"


def record_dir():
    explicit = os.environ.get("CONSOLE_RECORD_DIR")
    if explicit:
        return explicit
    return os.path.join(os.path.expanduser("~"), ".voice-ai", "console")


def _clock():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Hub:
    """轮次与日志的环形缓冲，带单调递增游标供前端增量拉取。"""

    def __init__(self, directory=None, record=None, max_turns=MAX_TURNS):
        self._lock = threading.Lock()
        self._turns = collections.deque(maxlen=max_turns)
        self._logs = collections.deque(maxlen=MAX_LOG_LINES)
        self._turn_seq = 0
        self._log_seq = 0
        self._started = time.time()
        self._dropped = 0
        self._record = record_enabled() if record is None else bool(record)
        self._path = None
        if self._record:
            folder = directory or record_dir()
            try:
                os.makedirs(folder, exist_ok=True)
                self._path = os.path.join(folder, "turns.jsonl")
            except OSError:
                self._path = None

    @property
    def path(self):
        return self._path

    @property
    def uptime_s(self):
        return round(time.time() - self._started, 1)

    def record(self, entry):
        """存一轮快照并落盘；返回带 seq 的副本（失败也不抛）。"""
        with self._lock:
            self._turn_seq += 1
            item = dict(entry)
            item["seq"] = self._turn_seq
            if self._turns.maxlen and len(self._turns) == self._turns.maxlen:
                self._dropped += 1
            self._turns.append(item)
            path = self._path
        if path:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return item

    def log(self, lines):
        """收一批日志行（通常是 report 的输出）。"""
        at = _clock()
        with self._lock:
            for line in lines:
                self._log_seq += 1
                self._logs.append({"seq": self._log_seq, "at": at, "text": line})

    def since(self, turn_seq=0, log_seq=0, limit=50):
        """返回游标之后的增量，附最新游标号供前端续拉。"""
        with self._lock:
            turns = [t for t in self._turns if t["seq"] > turn_seq]
            if limit and len(turns) > limit:
                turns = turns[-limit:]
            logs = [l for l in self._logs if l["seq"] > log_seq]
            cursor = {"turn_seq": self._turn_seq, "log_seq": self._log_seq}
            dropped = self._dropped
        return {"turns": turns, "logs": logs, "cursor": cursor, "dropped": dropped}

    def recent_turns(self, limit=50):
        with self._lock:
            return list(self._turns)[-limit:]

    def counts(self):
        with self._lock:
            return {"turns": self._turn_seq, "logs": self._log_seq,
                    "kept": len(self._turns)}


def snapshot(timers, turn, text=None, source="device", pcm_bytes=0):
    """把一轮的遥测压成可 JSON 化的扁平结构。"""
    marks = dict(getattr(timers, "marks", None) or {})
    stats = dict(getattr(turn, "stats", None) or {})
    abort = getattr(turn, "abort", None)
    audio_ms = int(pcm_bytes / BYTES_PER_MS) if pcm_bytes else 0
    return {
        "at": _clock(),
        "epoch_ms": int(time.time() * 1000),
        "source": source,
        "session": getattr(turn, "session_id", "anon"),
        "gen": getattr(turn, "gen", 0),
        "user": text if text is not None else (getattr(turn, "user_text", "") or ""),
        "reply": getattr(turn, "reply_text", "") or "",
        "error": getattr(turn, "error", None),
        "completed": bool(getattr(turn, "completed", False)),
        "aborted": bool(abort is not None and abort.is_set()),
        "local_command": getattr(turn, "local_command", "") or "",
        "local_plugin": bool(getattr(turn, "local_plugin", False)),
        "local_plugin_failed": bool(getattr(turn, "local_plugin_failed", False)),
        "marks": marks,
        "first_audio_ms": marks.get("first_byte"),
        "engine": stats.get("engine"),
        "tts_ms": stats.get("first_tts_ms"),
        "ttft": stats.get("first_token_seconds"),
        "chunks": stats.get("chunks"),
        "total_seconds": stats.get("total_seconds"),
        "chars": stats.get("content_chars"),
        "finish_reason": stats.get("finish_reason"),
        "profile": stats.get("profile"),
        "fallback_used": stats.get("fallback_used"),
        "attempts": _safe(stats.get("attempts")),
        "usage": _safe(stats.get("usage")),
        "reasoning_chars": stats.get("reasoning_chars"),
        "memory": _safe(getattr(turn, "memory_info", None)),
        "files": _safe(getattr(turn, "file_info", None)),
        "pcm_bytes": int(pcm_bytes or 0),
        "audio_ms": audio_ms,
    }


def _safe(value):
    """stats 里可能混进非 JSON 类型；宁可丢字段也不让落盘失败。"""
    if value is None:
        return None
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


_hub = None
_hub_lock = threading.Lock()


def hub():
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = Hub()
        return _hub


def reset(directory=None, record=False):
    """测试用：换一个干净的 hub。"""
    global _hub
    with _hub_lock:
        _hub = Hub(directory=directory, record=record)
        return _hub
