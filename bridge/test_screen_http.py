import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import screen_http


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    streamer = None
    hub = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/screen"):
            return screen_http.write_stream(self, self.streamer)
        self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/screen/key"):
            return screen_http.handle_key(self, self.hub)
        self.send_error(404)


def read_frames(resp, want):
    buf, frames = b"", []
    for chunk in resp.iter_content(4096):
        buf += chunk
        while True:
            if b"\r\n\r\n" not in buf:
                break
            head, rest = buf.split(b"\r\n\r\n", 1)
            n = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    n = int(line.split(b":")[1])
            if len(rest) < n + 2:
                break
            frames.append(rest[:n])
            buf = rest[n + 2:]
        if len(frames) >= want:
            break
    return frames


class StreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = screen_http.make_test_hub()
        cls.streamer = screen_http.Streamer(cls.hub, fps=20)
        cls.streamer.start()
        Handler.hub, Handler.streamer = cls.hub, cls.streamer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.streamer.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_stream_delivers_jpeg_frames(self):
        r = requests.get("http://127.0.0.1:%d/screen" % self.port, stream=True, timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertIn("multipart/x-mixed-replace", r.headers["Content-Type"])
        frames = read_frames(r, 3)
        r.close()
        self.assertGreaterEqual(len(frames), 3)
        self.assertEqual(frames[0][:3], b"\xff\xd8\xff")   # JPEG SOI

    def test_key_endpoint_sets_recording(self):
        r = requests.post("http://127.0.0.1:%d/screen/key" % self.port, timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "recording")

    def test_two_viewers_both_get_frames(self):
        r1 = requests.get("http://127.0.0.1:%d/screen" % self.port, stream=True, timeout=10)
        r2 = requests.get("http://127.0.0.1:%d/screen" % self.port, stream=True, timeout=10)
        f1 = read_frames(r1, 2)
        f2 = read_frames(r2, 2)
        r1.close()
        r2.close()
        self.assertGreaterEqual(len(f1), 2)
        self.assertGreaterEqual(len(f2), 2)

    def test_subscriber_is_released_after_close(self):
        r = requests.get("http://127.0.0.1:%d/screen" % self.port, stream=True, timeout=10)
        read_frames(r, 1)
        r.close()
        deadline = time.time() + 3
        while time.time() < deadline and self.streamer.subscriber_count() > 0:
            time.sleep(0.05)
        self.assertEqual(self.streamer.subscriber_count(), 0)


if __name__ == "__main__":
    unittest.main()
