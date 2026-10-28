"""调试台「歌曲」接口的测试：上传会写盘，所以只允许本机，且全程用临时目录。"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import server
import songs


def wav_bytes(seconds=0.5, rate=44100):
    import numpy as np
    import soundfile as sf
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    data = (0.2 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    buf = io.BytesIO()
    sf.write(buf, data, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class SongsHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "songs")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "playlist.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"songs": []}, fh)
        self.old = (songs.SONGS_DIR, songs.PLAYLIST)
        songs.SONGS_DIR = songs.Path(self.dir)
        songs.PLAYLIST = songs.SONGS_DIR / "playlist.json"
        songs.reset()
        self.addCleanup(self._restore)

    def _restore(self):
        songs.SONGS_DIR, songs.PLAYLIST = self.old
        songs.reset()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def test_requires_auth(self):
        import requests
        with patch.object(config, "auth_token", return_value="secret"):
            listing = requests.get(self.url("/api/songs"), timeout=5)
            uploading = requests.post(self.url("/api/songs?title=x&name=a.wav"),
                                      data=wav_bytes(), timeout=10)
        self.assertEqual(listing.status_code, 401)
        self.assertEqual(uploading.status_code, 401)

    def test_list_starts_empty(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            data = requests.get(self.url("/api/songs"), timeout=5).json()
        self.assertEqual(data["songs"], [])

    def test_upload_list_delete_roundtrip(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            up = requests.post(self.url("/api/songs"),
                               params={"title": "接口歌", "name": "api.wav"},
                               data=wav_bytes(), timeout=15)
            self.assertEqual(up.status_code, 200, up.text)
            entry = up.json()
            self.assertEqual(entry["title"], "接口歌")
            self.assertEqual(entry["file"], "api.pcm")
            self.assertNotIn("path", entry)

            rows = requests.get(self.url("/api/songs"), timeout=5).json()["songs"]
            self.assertEqual([r["title"] for r in rows], ["接口歌"])
            self.assertTrue(os.path.isfile(os.path.join(self.dir, "api.pcm")))

            gone = requests.post(self.url("/api/songs/delete"),
                                 json={"file": "api.pcm"}, timeout=5)
            self.assertEqual(gone.status_code, 200)
            self.assertEqual(
                requests.get(self.url("/api/songs"), timeout=5).json()["songs"], [])
            self.assertEqual(os.listdir(self.dir), ["playlist.json"])

    def test_upload_rejects_bad_extension(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            resp = requests.post(self.url("/api/songs"),
                                 params={"title": "x", "name": "notes.txt"},
                                 data=b"hello", timeout=5)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("wav", resp.json()["error"])

    def test_delete_unknown_is_404(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            resp = requests.post(self.url("/api/songs/delete"),
                                 json={"file": "nope.pcm"}, timeout=5)
        self.assertEqual(resp.status_code, 404)

    def test_rejects_non_loopback(self):
        import requests
        fake = type("Addr", (), {"is_loopback": False})()
        with patch.object(config, "auth_token", return_value=None):
            with patch.object(server.ipaddress, "ip_address", return_value=fake):
                listing = requests.get(self.url("/api/songs"), timeout=5)
                uploading = requests.post(self.url("/api/songs?name=a.wav"),
                                          data=wav_bytes(), timeout=10)
        self.assertEqual(listing.status_code, 403)
        self.assertEqual(uploading.status_code, 403)
        self.assertEqual(os.listdir(self.dir), ["playlist.json"])

    def test_console_panel_present(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            html = requests.get(self.url("/console"), timeout=5).text
        self.assertIn('id="btnSongs"', html)
        self.assertIn("showSongs", html)
        self.assertIn('api("/api/songs"', html)

    def test_console_spells_out_upload_rules(self):
        import requests
        with patch.object(config, "auth_token", return_value=None):
            html = requests.get(self.url("/console"), timeout=5).text
        self.assertIn("加入歌单的要求", html)
        for word in ("wav", "mp3", "flac", "ogg", "m4a", "32 MB", "10 分钟", "留空就用文件名"):
            self.assertIn(word, html, word)
        self.assertIn("服务端还是旧代码", html)      # 404 时提示重启，而不是静默失败


if __name__ == "__main__":
    unittest.main()
