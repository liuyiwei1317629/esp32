import io
import os
import unittest

import numpy as np
from PIL import Image, ImageFont

import screen_compose as sc


class ComposeTests(unittest.TestCase):
    def test_output_is_320x240_jpeg(self):
        blob = sc.compose(None, "按一下就说话")
        im = Image.open(io.BytesIO(blob))
        self.assertEqual(im.format, "JPEG")
        self.assertEqual(im.size, (320, 240))

    def test_text_lands_in_bottom_bar_and_keeps_margin(self):
        blank = Image.new("RGB", (320, 180), (0, 0, 0))
        blob = sc.compose(blank, "正在播放 · 一首名字特别特别长的中文歌曲名字测试")
        a = np.asarray(Image.open(io.BytesIO(blob)).convert("L"))
        self.assertLess(a[0:180, :].max(), 24)          # 动画区仍是黑（纯黑输入）
        rows = np.where(a[180:240].max(axis=1) > 100)[0] + 180
        self.assertGreater(rows.min(), 184)             # 不压到动画区
        self.assertLessEqual(rows.max(), 235)           # 两行都留出下边距

    def test_animation_area_is_used(self):
        red = Image.new("RGB", (320, 180), (255, 0, 0))
        a = np.asarray(Image.open(io.BytesIO(sc.compose(red, ""))).convert("RGB"))
        self.assertGreater(a[10:170, 10:310, 0].mean(), 150)

    def test_wrap_splits_long_song_names(self):
        font = ImageFont.truetype(sc.FONT_HINT.path, 18)
        lines = sc.wrap("正在播放 · 一首名字特别特别长的歌曲名字测试", font, 300, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(l) <= 30 for l in lines))

    def test_wrap_keeps_short_text_on_one_line(self):
        self.assertEqual(sc.wrap("正在听…", sc.FONT_HINT, 304, 2), ["正在听…"])

    def test_real_frame_compresses_small(self):
        frame = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "screen_frames", "待机呼吸休闲", "0001.jpg")
        anim = Image.open(frame)
        blob = sc.compose(anim, "按一下就说话")
        self.assertLess(len(blob), 30000)


if __name__ == "__main__":
    unittest.main()
