import json
import os
import sys
import unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FRAMES = os.path.join(HERE, "screen_frames")

ANIMS = ["待机呼吸休闲", "东张西望", "工作状态-思考冒泡", "悠闲哼歌",
         "工作状态-垂头叹气冒汗", "点击回应-傲娇生气", "原地小憩沉眠",
         "超大伸懒腰", "照镜子", "撸猫", "打瞌睡被惊醒", "摇扇纳凉",
         "原地蹲下玩玩具汽车", "大口吃零食", "碎碎念-发呆碎碎念",
         "鲸鱼吐泡泡特效", "轻快摇摆舞", "点击回应-开心跃动",
         "工作状态-雀跃庆祝", "可爱宅舞", "哈欠连天", "被吓一跳",
         "点击回应-害羞惊讶", "点击回应-元气挥手", "点击回应-挠痒咯咯笑"]


def load_index():
    with open(os.path.join(FRAMES, "index.json"), encoding="utf-8") as fh:
        return json.load(fh)


class FrameLibraryTests(unittest.TestCase):
    def test_index_lists_all_25_anims(self):
        self.assertEqual(sorted(load_index()), sorted(ANIMS))

    def test_every_frame_is_320x180_jpeg(self):
        index = load_index()
        for name, meta in index.items():
            first = os.path.join(FRAMES, name, "0001.jpg")
            im = Image.open(first)
            self.assertEqual(im.size, (320, 180), name)
            self.assertEqual(im.format, "JPEG", name)
            self.assertGreaterEqual(meta["frames"], 10, name)

    def test_background_is_black_not_white(self):
        index = load_index()
        for name, meta in index.items():
            path = os.path.join(FRAMES, name, "%04d.jpg" % meta["frames"])
            a = np.asarray(Image.open(path).convert("RGB"))
            corner = a[0:6, 0:6].mean()
            self.assertLess(corner, 12.0, "%s corner=%.1f（白底没压掉？）" % (name, corner))


if __name__ == "__main__":
    unittest.main()
