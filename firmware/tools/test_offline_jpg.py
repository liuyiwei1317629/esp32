import io
import os
import re
import sys
import unittest

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
HEADER = os.path.join(HERE, "..", "voice_terminal", "offline_jpg.h")


def load_bytes():
    with open(HEADER, encoding="utf-8") as fh:
        text = fh.read()
    body = re.search(r"kOfflineJpg\[\]\s*=\s*\{(.*?)\};", text, re.S).group(1)
    return bytes(int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]{2})", body))


class OfflineJpgTests(unittest.TestCase):
    def test_size_under_budget(self):
        self.assertLessEqual(len(load_bytes()), 40000)

    def test_decodes_to_320x240(self):
        im = Image.open(io.BytesIO(load_bytes()))
        self.assertEqual(im.size, (320, 240))

    def test_length_constant_matches(self):
        with open(HEADER, encoding="utf-8") as fh:
            text = fh.read()
        n = int(re.search(r"kOfflineJpgLen\s*=\s*(\d+)", text).group(1))
        self.assertEqual(n, len(load_bytes()))


if __name__ == "__main__":
    unittest.main()
