import base64
import json
import os
import queue
import subprocess
import threading
import time

import config

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")


class TtsWorker:
    """A resident TTS subprocess speaking JSON-lines over stdin/stdout.

    Requests are matched by id, so several sentences can sit in the worker
    queue at once while it renders them one by one.
    """

    def __init__(self, name, python, script, ready_timeout):
        self.name = name
        self.ready = False
        self.info = {}
        self._lock = threading.Lock()
        self._pending = {}
        self._seq = 0
        self._dead = None
        self._ready_evt = threading.Event()
        os.makedirs(LOG_DIR, exist_ok=True)
        self._log = open(os.path.join(LOG_DIR, name + ".log"), "wb")
        # The workers write their own UTF-8 protocol bytes; this covers the
        # model's and edge-tts's own prints, which would otherwise hit GBK.
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self._proc = subprocess.Popen(
            [python, os.path.join(HERE, script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, cwd=HERE, env=env)
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        self._ready_evt.wait(ready_timeout)

    @property
    def dead_reason(self):
        return self._dead

    @property
    def pid(self):
        return self._proc.pid

    def _pump_stderr(self):
        for chunk in iter(lambda: self._proc.stderr.readline(), b""):
            self._log.write(chunk)
            self._log.flush()

    def _pump_stdout(self):
        for raw in self._proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("ready"):
                self.info = msg
                self.ready = True
                self._ready_evt.set()
                continue
            target = self._pending.pop(msg.get("id"), None)
            if target is not None:
                target.put(msg)
        self._dead = "worker exited (code %s)" % self._proc.poll()
        self._ready_evt.set()
        for target in list(self._pending.values()):
            target.put({"ok": False, "err": self._dead})
        self._pending.clear()

    def request(self, payload, timeout):
        if self._dead:
            raise RuntimeError(self._dead)
        with self._lock:
            if self._dead:
                raise RuntimeError(self._dead)
            self._seq += 1
            rid = self._seq
            box = queue.Queue()
            self._pending[rid] = box
            blob = json.dumps(dict(payload, id=rid), ensure_ascii=False).encode("utf-8") + b"\n"
            self._proc.stdin.write(blob)
            self._proc.stdin.flush()
        try:
            msg = box.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(rid, None)
            raise
        self._pending.pop(rid, None)
        if not msg.get("ok"):
            raise RuntimeError(msg.get("err", "unknown worker failure"))
        return base64.b64decode(msg["b64"]), msg["ms"], msg["samples"]

    def stop(self):
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        self._proc.terminate()
        self._log.close()


class Tts:
    """CosyVoice on the main path, edge-tts as the timeout/error fallback."""

    def __init__(self, start_cosyvoice=True):
        self.edge = TtsWorker("edge", config.SYSTEM_PY, "worker_edge.py", 45)
        self.cosy = None
        if start_cosyvoice:
            threading.Thread(target=self._start_cosy, daemon=True).start()

    def _start_cosy(self):
        self.cosy = TtsWorker("cosyvoice", config.PY311, "worker_cosyvoice.py", 180)

    def synthesize(self, text):
        cosy = self.cosy
        if cosy is not None and cosy.ready:
            t0 = time.perf_counter()
            try:
                pcm, ms, samples = cosy.request({"text": text, "spk": config.COSYVOICE_SPEAKER},
                                                config.COSYVOICE_TIMEOUT)
                return pcm, {"engine": "cosyvoice", "tts_ms": ms, "samples": samples,
                             "fallback": False}
            except (queue.Empty, TimeoutError):
                pass
            except RuntimeError:
                pass
        t0 = time.perf_counter()
        pcm, ms, samples = self.edge.request({"text": text, "voice": config.EDGE_VOICE}, 20)
        return pcm, {"engine": "edge", "tts_ms": ms, "samples": samples,
                     "fallback": cosy is not None and cosy.ready,
                     "waited_ms": round((time.perf_counter() - t0) * 1000)}

    def stop(self):
        self.edge.stop()
        if self.cosy is not None:
            self.cosy.stop()
