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

    def __init__(self, name, python, script, ready_timeout, args=None):
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
        # script=None means `python` is a launcher (wsl.exe) and args carries
        # the whole Linux command line.
        cmd = [python] + ([os.path.join(HERE, script)] if script else []) + list(args or [])
        self._proc = subprocess.Popen(
            cmd,
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
    """Feiyu (小鱼, in WSL) on the main path, CosyVoice then edge-tts behind it."""

    def __init__(self, start_cosyvoice=True, start_feiyu=None):
        if start_feiyu is None:
            start_feiyu = config.FEIYU_ENABLED
        self.edge = TtsWorker("edge", config.SYSTEM_PY, "worker_edge.py", 45)
        self.feiyu = None
        self.cosy = None
        if start_feiyu:
            threading.Thread(target=self._start_feiyu, daemon=True).start()
        elif start_cosyvoice:
            # Feiyu (3.4 GB) and CosyVoice would not both fit in 6 GB of VRAM.
            threading.Thread(target=self._start_cosy, daemon=True).start()

    def _start_feiyu(self):
        self.feiyu = TtsWorker(
            "feiyu", config.FEIYU_WSL, None, config.FEIYU_READY_TIMEOUT,
            args=["-d", config.FEIYU_DISTRO, "--", "/usr/bin/env",
                  "FEIYU_MODEL_DIR=" + config.FEIYU_MODEL_DIR,
                  "FEIYU_STYLE=" + config.FEIYU_STYLE,
                  config.FEIYU_PY, config.FEIYU_SCRIPT])

    def _start_cosy(self):
        self.cosy = TtsWorker("cosyvoice", config.PY311, "worker_cosyvoice.py", 180)

    def synthesize(self, text):
        engines = (
            (self.feiyu, "feiyu", config.FEIYU_TIMEOUT, {}),
            (self.cosy, "cosyvoice", config.COSYVOICE_TIMEOUT, {"spk": config.COSYVOICE_SPEAKER}),
        )
        t0 = time.perf_counter()
        for worker, engine, timeout, payload in engines:
            if worker is None or not worker.ready:
                continue
            try:
                pcm, ms, samples = worker.request(dict(payload, text=text), timeout)
                return pcm, {"engine": engine, "tts_ms": ms, "samples": samples,
                             "fallback": False}
            except (queue.Empty, TimeoutError):
                pass
            except RuntimeError:
                pass
        pcm, ms, samples = self.edge.request({"text": text, "voice": config.EDGE_VOICE}, 20)
        return pcm, {"engine": "edge", "tts_ms": ms, "samples": samples,
                     "fallback": any(w is not None and w.ready for w, _, _, _ in engines),
                     "waited_ms": round((time.perf_counter() - t0) * 1000)}

    def stop(self):
        self.edge.stop()
        if self.feiyu is not None:
            self.feiyu.stop()
            # terminate() 只杀 Windows 侧的 wsl.exe，Linux 侧的 worker 会活着
            # 继续占 3.4 GB 显存；这里补一刀，否则下次启动会因显存不足起不来。
            _reap_feiyu_worker()
        if self.cosy is not None:
            self.cosy.stop()


def _reap_feiyu_worker():
    try:
        subprocess.run(
            [config.FEIYU_WSL, "-d", config.FEIYU_DISTRO, "-u", "root",
             "--exec", "pkill", "-f", "worker_feiyu.py"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass
