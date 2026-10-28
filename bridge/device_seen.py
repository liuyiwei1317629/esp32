"""设备最近一次露面：哪个 IP、什么时候。

设备只在说话/按键时才会发 /turn，所以这里没有「在线」概念，
只有「最近一次见到」。只存 IP 和时间，不碰录音内容。
"""

import json
import os
import tempfile
import threading
import time

# 每轮都写盘没必要；同一 IP 至少隔这么久才落一次
WRITE_EVERY_S = 30


class DeviceSeen:
    def __init__(self, path, clock=time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._ip = None
        self._ts = None
        self._saved_ts = None
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._ip = str(data.get("ip") or "") or None
            self._ts = float(data.get("last_seen_ts") or 0) or None
            self._saved_ts = self._ts
        except (OSError, ValueError, TypeError):
            # 没有文件或写坏了都当「从未见过」；这只是展示用的状态
            self._ip = self._ts = self._saved_ts = None

    def record(self, ip):
        ip = (ip or "").strip()
        if not ip:
            return
        with self._lock:
            now = self._clock()
            changed = ip != self._ip
            self._ip, self._ts = ip, now
            if changed or self._saved_ts is None or now - self._saved_ts >= WRITE_EVERY_S:
                self._save(now)

    def _save(self, now):
        data = {
            "ip": self._ip,
            "last_seen_ts": self._ts,
            "last_seen": _stamp(self._ts),
        }
        folder = os.path.dirname(self.path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".device-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._saved_ts = now
        except OSError:
            # 状态展示写不进去不影响主流程，下轮再试
            pass

    def status(self):
        with self._lock:
            if not self._ip or not self._ts:
                return None
            return {
                "ip": self._ip,
                "age_s": max(0, int(self._clock() - self._ts)),
                "last_seen": _stamp(self._ts),
            }


def _stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
