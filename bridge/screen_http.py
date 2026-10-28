"""MJPEG 推流 + 按键事件路由。

协议（设备端照这个解析）：
  200
  Content-Type: multipart/x-mixed-replace; boundary=frame
  Connection: close
  <body 是 close-delimited 的裸流>
  --frame\\r\\nContent-Type: image/jpeg\\r\\nContent-Length: N\\r\\nX-State: idle\\r\\n\\r\\n<N 字节 JPEG>\\r\\n

用「连接关闭即结束」而不是 chunked，设备端少一层解析。
"""
import json
import queue
import threading
import time

import screen_config as sc


class Streamer:
    def __init__(self, hub, fps=None):
        self.hub = hub
        self.fps = fps or sc.FPS
        self._subs = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def subscribe(self):
        q = queue.Queue(maxsize=2)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def subscriber_count(self):
        with self._lock:
            return len(self._subs)

    def broadcast(self, blob):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            if q.full():
                try:
                    q.get_nowait()          # 丢最旧的，永远给最新帧
                except queue.Empty:
                    pass
            try:
                q.put_nowait(blob)
            except queue.Full:
                pass

    def _run(self):
        period = 1.0 / float(self.fps)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.hub.tick()
                if self.subscriber_count():      # 没人看就不合成，省 CPU
                    self.broadcast(self.hub.frame_jpeg())
            except Exception as exc:             # 一帧失败不能杀掉推流
                print("  WARN screen frame: %s: %s" % (type(exc).__name__, exc))
            self._stop.wait(max(0.0, period - (time.monotonic() - t0)))

    def start(self):
        self._thread = threading.Thread(target=self._run, name="screen-stream",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


def write_stream(handler, streamer):
    q = streamer.subscribe()
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.close_connection = True
        handler.end_headers()
        while True:
            try:
                blob = q.get(timeout=5)
            except queue.Empty:
                break                            # 5 s 没新帧：收工，设备自己重连
            state = streamer.hub.state.encode("ascii", "replace")
            handler.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                b"Content-Length: %d\r\nX-State: %s\r\n\r\n"
                                % (len(blob), state))
            handler.wfile.write(blob)
            handler.wfile.write(b"\r\n")
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        streamer.unsubscribe(q)


def handle_key(handler, hub):
    hub.on_key()
    blob = json.dumps({"state": hub.state, "anim": hub.anim},
                      ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(blob)))
    handler.end_headers()
    handler.wfile.write(blob)


def make_test_hub():
    import screen_hub
    return screen_hub.ScreenHub(lib=None)
