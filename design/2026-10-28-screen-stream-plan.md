# 小鱼屏幕：固件 + 推流 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 2.4 寸 ST7789 屏由电脑推帧显示：状态动画 + 底部文字条 + 空闲插播 + 按键反应 + 断线离线图。

**Architecture:** 电脑端（bridge）预转码 25 段动画成 JPEG 帧序列，运行时按状态合成「动画 + 两行文字」成 320×240 JPEG，用一条长连接 `GET /screen` 以 multipart 推给设备；设备独立任务收流、用 TJpg_Decoder 解码进 PSRAM 帧缓冲、整帧刷屏。按键事件由设备反向 `POST /screen/key` 通知电脑（不阻塞录音）。

**Tech Stack:** Python 3.13（bridge venv，需新增 Pillow）+ 标准库 ThreadingHTTPServer；WSL Ubuntu-24.04 的 ffmpeg 6.1.1（仅构建期转码）；Arduino-ESP32 3.3.12 + TJpg_Decoder（新增唯一第三方库）+ 手写 ST7789 最小驱动。

**Spec:** `C:\Users\DELL\Documents\deepseek-harness\default-workspace\esp32肥鱼\屏幕接线单页-2026-10-28.md`（§5 版式、§5A 显示策略、§6 推流、§7 flash 账）

## Global Constraints

- **代码写在 `C:\Users\DELL\voice-ai`（真正在跑的工程）**；工作区 `esp32肥鱼/` 只是副本，改完不自动同步，需要时另行拷贝说明。
- **不改音频参数**：`CLIP_TARGET_RMS`、`CLIP_GAIN_MAX`、`PLAY_STREAM_BYTES*`、`PLAY_PREBUFFER_BYTES` 一律不动；屏幕任务不得降低音频优先级（音频任务 core 1 / prio 4，屏幕任务 core 0 / prio 2）。
- **不烧录**（屏未到货）、**不提交 git**（未授权；仓库有 44 项未提交改动，一律不碰 `git add/commit`）。
- **不读 `secrets.h`、不打印 token**；`X-Auth` 只从现有 `BRIDGE_TOKEN` 机械透传。
- **不换分区、不用 SD、不引入 LVGL**；新增第三方库只允许 TJpg_Decoder 一个（约 10–15 KB flash，估算）。
- **屏幕方向**：屏 240×320，**横屏用 320×240**；动画区 320×180 + 底部文字条 60 px。
- **按键反应不得延迟录音**：设备主循环只置标志，任何联网动作都在屏幕任务里做。
- 一切尺寸/耗时数字必须标「实测」或「估算」；本计划里未跑过的数字都写「估算」。

---

### Task A1: 动画帧库构建工具

**Files:**
- Create: `bridge/tools/build_screen_frames.py`
- Create: `bridge/screen_frames/<动画名>/0001.jpg …`（产物，约 7–10 MB，估算）
- Create: `bridge/screen_frames/index.json`
- Test: `bridge/test_screen_frames_build.py`

**Interfaces:**
- Produces: `bridge/screen_frames/index.json` = `{"<动画名>": {"frames": int, "fps": int}}`；帧文件统一 320×180、JPEG、黑底。

- [ ] **Step 1: 写失败的测试**

`bridge/test_screen_frames_build.py`：

```python
import json
import os
import unittest

import numpy as np
from PIL import Image

FRAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_frames")

ANIMS = ["待机呼吸休闲", "东张西望", "工作状态-思考冒泡", "悠闲哼歌",
         "工作状态-垂头叹气冒汗", "点击回应-傲娇生气", "原地小憩沉眠",
         "超大伸懒腰", "照镜子", "撸猫", "打瞌睡被惊醒", "摇扇纳凉",
         "原地蹲下玩玩具汽车", "大口吃零食", "碎碎念-发呆碎碎念",
         "鲸鱼吐泡泡特效", "轻快摇摆舞", "点击回应-开心跃动",
         "工作状态-雀跃庆祝", "可爱宅舞", "哈欠连天", "被吓一跳",
         "点击回应-害羞惊讶", "点击回应-元气挥手", "点击回应-挠痒咯咯笑"]


class FrameLibraryTests(unittest.TestCase):
    def test_index_lists_all_25_anims(self):
        with open(os.path.join(FRAMES, "index.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        self.assertEqual(sorted(index), sorted(ANIMS))

    def test_every_frame_is_320x180_jpeg(self):
        with open(os.path.join(FRAMES, "index.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        for name, meta in index.items():
            first = os.path.join(FRAMES, name, "0001.jpg")
            im = Image.open(first)
            self.assertEqual(im.size, (320, 180), name)
            self.assertEqual(im.format, "JPEG", name)
            self.assertGreaterEqual(meta["frames"], 10, name)

    def test_background_is_black_not_white(self):
        with open(os.path.join(FRAMES, "index.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        for name, meta in index.items():
            path = os.path.join(FRAMES, name, "%04d.jpg" % meta["frames"])
            a = np.asarray(Image.open(path).convert("RGB"))
            corner = a[0:6, 0:6].mean()
            self.assertLess(corner, 12.0, "%s corner=%.1f（白底没压掉？）" % (name, corner))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd C:/Users/DELL/voice-ai && ./.venv/Scripts/python.exe -m unittest bridge.test_screen_frames_build -v
```
Expected: FAIL（`screen_frames/index.json` 不存在）

- [ ] **Step 3: 写构建脚本**

`bridge/tools/build_screen_frames.py`：

```python
# -*- coding: utf-8 -*-
"""把桌宠包的 webm 转成 320x180 JPEG 帧序列，供推流用。

只做构建期转换：运行时 bridge 不依赖 ffmpeg（Windows 上没有）。
用 WSL 的 ffmpeg 6.1.1（本机实测可用），转完再拷回 Windows。
"""
import json
import os
import shutil
import subprocess
import tempfile

SRC_WSL = "/mnt/c/Users/DELL/Desktop/dsh项目记忆后端+pet/cute-fat-fish-pet-1.2.3-win-x64/app/assets/webm"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "screen_frames")
FPS = 10
W, H = 320, 180

ANIMS = [
    "待机呼吸休闲", "东张西望", "工作状态-思考冒泡", "悠闲哼歌",
    "工作状态-垂头叹气冒汗", "点击回应-傲娇生气", "原地小憩沉眠",
    "超大伸懒腰", "照镜子", "撸猫", "打瞌睡被惊醒", "摇扇纳凉",
    "原地蹲下玩玩具汽车", "大口吃零食", "碎碎念-发呆碎碎念",
    "鲸鱼吐泡泡特效", "轻快摇摆舞", "点击回应-开心跃动",
    "工作状态-雀跃庆祝", "可爱宅舞", "哈欠连天", "被吓一跳",
    "点击回应-害羞惊讶", "点击回应-元气挥手", "点击回应-挠痒咯咯笑",
]


def convert(name, tmpdir):
    src = "%s/%s.webm" % (SRC_WSL, name)
    dst_wsl = "/mnt/" + tmpdir.replace("\\", "/").replace("C:", "c", 1).replace(":/", "/") + "/"
    vf = ("fps=%d,scale=%d:%d:flags=lanczos,format=rgba[fg];"
          "color=black:s=%dx%d:r=%d[bg];[bg][fg]overlay=shortest=1,format=yuvj420p"
          % (FPS, W, H, W, H, FPS))
    cmd = ["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc",
           "ffmpeg -loglevel error -y -i '%s' -an -filter_complex \"%s\" -q:v 3 -start_number 1 '%s%%04d.jpg'"
           % (src, vf, dst_wsl)]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    subprocess.run(cmd, check=True, env=env)


def main():
    os.makedirs(OUT, exist_ok=True)
    index = {}
    for name in ANIMS:
        tmpdir = tempfile.mkdtemp(prefix="frames_")
        try:
            convert(name, tmpdir)
            files = sorted(f for f in os.listdir(tmpdir) if f.endswith(".jpg"))
            if not files:
                raise SystemExit("no frames for %s" % name)
            dst = os.path.join(OUT, name)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            os.makedirs(dst)
            for f in files:
                shutil.move(os.path.join(tmpdir, f), os.path.join(dst, f))
            index[name] = {"frames": len(files), "fps": FPS}
            print("%-18s %3d frames" % (name, len(files)))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 先给 bridge venv 装 Pillow（唯一新增 Python 依赖）**

```bash
cd C:/Users/DELL/voice-ai && NO_PROXY="*" HTTP_PROXY="" HTTPS_PROXY="" ./.venv/Scripts/python.exe -m pip install pillow
```
（本机代理 127.0.0.1:7890 已失效，必须清掉代理环境变量；实测这条能下到 PyPI。）

- [ ] **Step 5: 跑构建**

```bash
cd C:/Users/DELL/voice-ai && ./.venv/Scripts/python.exe bridge/tools/build_screen_frames.py
```
Expected: 25 行 `xxx  N frames`，每段 20–60 帧（3–6 s × 10 fps）

- [ ] **Step 6: 跑测试确认通过**

```bash
cd C:/Users/DELL/voice-ai && ./.venv/Scripts/python.exe -m unittest bridge.test_screen_frames_build -v
```
Expected: 3 个测试 PASS。若黑底测试失败，说明 alpha 没有被压黑，把 `overlay` 一段换成 `[fg]split[fg2][a];[a]alphaextract[al];[bg][fg2][al]alphamerge[o];[o]overlay`。

- [ ] **不提交**（未授权）。

---

### Task A2: 横屏关机图（320×240）与固件用离线图头文件

**Files:**
- Modify: `C:\Users\DELL\Documents\deepseek-harness\default-workspace\esp32肥鱼\素材分类\使用中\01-关机或离线\`（重出 320×240 版，旧的 240×320 保留改名）
- Create: `C:\Users\DELL\voice-ai\bridge\screen_assets\offline_320x240.jpg`
- Create: `C:\Users\DELL\voice-ai\firmware\tools\gen_offline_jpg.py`
- Create: `C:\Users\DELL\voice-ai\firmware\voice_terminal\offline_jpg.h`
- Test: `firmware/tools/test_offline_jpg.py`

**Interfaces:**
- Produces: `offline_jpg.h` 里 `const uint8_t kOfflineJpg[]` + `const size_t kOfflineJpgLen`，内容为 320×240 JPEG，≤ 40,000 B。

> ⚠️ 前一轮做的关机图是 **240×320 竖屏**，跟定稿版式（320×180 动画 + 60 px 条 = 320×240 横屏）方向不符，本任务重做。

- [ ] **Step 1: 写失败的测试**

`firmware/tools/test_offline_jpg.py`：

```python
import os
import re
import unittest
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
HEADER = os.path.join(HERE, "..", "voice_terminal", "offline_jpg.h")


def load_bytes():
    text = open(HEADER, encoding="utf-8").read()
    body = re.search(r"kOfflineJpg\[\]\s*=\s*\{(.*?)\};", text, re.S).group(1)
    return bytes(int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]{2})", body))


class OfflineJpgTests(unittest.TestCase):
    def test_size_under_budget(self):
        self.assertLessEqual(len(load_bytes()), 40000)

    def test_decodes_to_320x240(self):
        import io
        im = Image.open(io.BytesIO(load_bytes()))
        self.assertEqual(im.size, (320, 240))

    def test_length_constant_matches(self):
        text = open(HEADER, encoding="utf-8").read()
        n = int(re.search(r"kOfflineJpgLen\s*=\s*(\d+)", text).group(1))
        self.assertEqual(n, len(load_bytes()))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd C:/Users/DELL/voice-ai && ./.venv/Scripts/python.exe -m unittest firmware.tools.test_offline_jpg -v
```
Expected: FAIL（header 不存在）

- [ ] **Step 3: 重出 320×240 关机图**

```python
# 存为 C:\Users\DELL\AppData\Local\Temp\shutdown_320.py，用系统 Python（有 Pillow）跑
import os
from PIL import Image, ImageDraw, ImageFont

CAT = r"C:\Users\DELL\Documents\deepseek-harness\default-workspace\esp32肥鱼\素材分类\使用中\01-关机或离线"
art = Image.open(os.path.join(CAT, "..", "..", "未使用", "静态图-黑底", "深度睡眠-透明.png"))
art = art.crop((0, 0, art.width, 366))            # 去掉水印行（角色到 364）
scale = 205 / art.height
art = art.resize((int(art.width * scale), 205), Image.LANCZOS)

W, H = 320, 240
canvas = Image.new("RGB", (W, H), (0, 0, 0))
canvas.paste(art, ((W - art.width) // 2, 0), art)
d = ImageDraw.Draw(canvas)
f = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 26)
txt = "电脑关机了"
bb = d.textbbox((0, 0), txt, font=f)
d.text(((W - (bb[2] - bb[0])) // 2, 212), txt, font=f, fill=(214, 220, 235))
canvas.save(os.path.join(CAT, "关机图-320x240.png"))
canvas.save(os.path.join(CAT, "关机图-320x240.jpg"), quality=86, subsampling=0)
os.makedirs(r"C:\Users\DELL\voice-ai\bridge\screen_assets", exist_ok=True)
canvas.save(r"C:\Users\DELL\voice-ai\bridge\screen_assets\offline_320x240.jpg", quality=86, subsampling=0)
print("ok")
```

- [ ] **Step 4: 写头文件生成脚本**

`firmware/tools/gen_offline_jpg.py`：

```python
# -*- coding: utf-8 -*-
"""把离线图 JPEG 转成 C 数组。用法：python gen_offline_jpg.py <jpg> <out.h>"""
import os
import sys

def main(src, dst):
    blob = open(src, "rb").read()
    if len(blob) > 40000:
        raise SystemExit("too big: %d B" % len(blob))
    lines = ["// 自动生成，别手改：firmware/tools/gen_offline_jpg.py",
             "#pragma once", "#include <stdint.h>", "#include <stddef.h>", "",
             "const uint8_t kOfflineJpg[] = {"]
    for i in range(0, len(blob), 16):
        lines.append("  " + ",".join("0x%02X" % b for b in blob[i:i + 16]) + ",")
    lines += ["};", "const size_t kOfflineJpgLen = %d;" % len(blob), ""]
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("wrote %s (%d B)" % (dst, len(blob)))

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

- [ ] **Step 5: 生成并跑测试**

```bash
cd C:/Users/DELL/voice-ai && ./.venv/Scripts/python.exe firmware/tools/gen_offline_jpg.py bridge/screen_assets/offline_320x240.jpg firmware/voice_terminal/offline_jpg.h && ./.venv/Scripts/python.exe -m unittest firmware.tools.test_offline_jpg -v
```
Expected: 3 PASS；打印字节数约 16,000–26,000 B（估算）

---

### Task B1: 屏幕配置项

**Files:**
- Create: `bridge/screen_config.py`
- Test: `bridge/test_screen_config.py`

**Interfaces:**
- Produces: `SCREEN_ENABLED: bool`、`FPS: int`、`QUALITY: int`、`IDLE_ENTER_S: tuple[int,int]`、`IDLE_EVERY_S: tuple[int,int]`、`KEY_REACTION_PROB: float`、`REACTION_S: tuple[float,float]`、`FRAMES_DIR: str`、`FONT_PATH: str`、`OFFLINE_JPG: str`

- [ ] **Step 1: 写失败的测试**

```python
import unittest

import screen_config as sc


class ConfigTests(unittest.TestCase):
    def test_defaults_are_sane(self):
        self.assertGreaterEqual(sc.FPS, 5)
        self.assertLessEqual(sc.FPS, 15)
        self.assertGreaterEqual(sc.QUALITY, 60)
        self.assertLessEqual(sc.QUALITY, 90)

    def test_idle_windows_are_ordered(self):
        lo, hi = sc.IDLE_ENTER_S
        self.assertLess(lo, hi)
        lo, hi = sc.IDLE_EVERY_S
        self.assertLess(lo, hi)
        self.assertEqual((lo, hi), (180, 300))

    def test_reaction_probability_is_sixty_percent(self):
        self.assertEqual(sc.KEY_REACTION_PROB, 0.6)

    def test_paths_exist(self):
        import os
        self.assertTrue(os.path.isdir(sc.FRAMES_DIR))
        self.assertTrue(os.path.isfile(sc.FONT_PATH))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败** → `FAIL ModuleNotFoundError: screen_config`

- [ ] **Step 3: 实现**

```python
"""屏幕推流的全部可调项。默认关闭：不改环境变量时 bridge 行为与现在完全一致。"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip() not in ("0", "", "false", "False")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


SCREEN_ENABLED = _env_bool("SCREEN_ENABLED", False)
FPS = _env_int("SCREEN_FPS", 10)
QUALITY = _env_int("SCREEN_QUALITY", 75)
IDLE_ENTER_S = (60, 120)         # 空闲多久开始随机插播
IDLE_EVERY_S = (180, 300)        # 之后每 3–5 分钟插一个
KEY_REACTION_PROB = 0.6
REACTION_S = (1.0, 2.0)
REPEAT_WINDOW_S = 30 * 60        # 同一段 30 分钟内不重复
FRAMES_DIR = os.path.join(_HERE, "screen_frames")
FONT_PATH = os.environ.get("SCREEN_FONT", r"C:\Windows\Fonts\msyh.ttc")
OFFLINE_JPG = os.path.join(_HERE, "screen_assets", "offline_320x240.jpg")
```

- [ ] **Step 4: 跑测试确认通过** → 4 PASS

---

### Task B2: 帧合成（动画 + 文字条）

**Files:**
- Create: `bridge/screen_compose.py`
- Test: `bridge/test_screen_compose.py`

**Interfaces:**
- Consumes: `screen_config.QUALITY`
- Produces: `compose(anim: PIL.Image | None, status: str, hint: str) -> bytes`；`wrap(text, font, max_w, max_lines=2) -> list[str]`

- [ ] **Step 1: 写失败的测试**

```python
import io
import unittest

import numpy as np
from PIL import Image

import screen_compose as sc


class ComposeTests(unittest.TestCase):
    def test_output_is_320x240_jpeg(self):
        blob = sc.compose(None, "IP · ONLINE", "按一下就说话")
        im = Image.open(io.BytesIO(blob))
        self.assertEqual(im.format, "JPEG")
        self.assertEqual(im.size, (320, 240))

    def test_text_lands_in_bottom_bar(self):
        blank = Image.new("RGB", (320, 180), (0, 0, 0))
        blob = sc.compose(blank, "", "正在播放 · 小情歌")
        a = np.asarray(Image.open(io.BytesIO(blob)).convert("L"))
        self.assertLess(a[0:180, :].max(), 24)          # 动画区仍是黑（纯黑输入）
        self.assertGreater(a[196:236, :].max(), 120)    # 文字条有亮像素

    def test_animation_area_is_used(self):
        red = Image.new("RGB", (320, 180), (255, 0, 0))
        a = np.asarray(Image.open(io.BytesIO(sc.compose(red, "", ""))).convert("RGB"))
        self.assertGreater(a[10:170, 10:310, 0].mean(), 150)

    def test_wrap_splits_long_song_names(self):
        from PIL import ImageFont
        font = ImageFont.truetype(sc.FONT_HINT.path, 18)
        lines = sc.wrap("正在播放 · 一首名字特别特别长的歌曲名字测试", font, 300, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(l) <= 30 for l in lines))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

```python
"""把「动画帧 + 两行文字」合成成一张 320x240 JPEG。纯函数，方便单测。"""
import io

from PIL import Image, ImageDraw, ImageFont

import screen_config as sc

W, H = 320, 240
ANIM_H = 180
BAR_Y = ANIM_H
FONT_STATUS = ImageFont.truetype(sc.FONT_PATH, 12)
FONT_HINT = ImageFont.truetype(sc.FONT_PATH, 18)

STATUS_COLOR = (150, 158, 176)
HINT_COLOR = (236, 238, 245)


def wrap(text, font, max_w, max_lines=2):
    """按像素宽度贪心折行；超出 max_lines 的部分丢弃（宁可截断也不缩小字号）。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        trial = cur + ch
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = ch
        if len(lines) == max_lines:
            return lines[:max_lines]
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def compose(anim, status, hint):
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    if anim is not None:
        canvas.paste(anim.convert("RGB").resize((W, ANIM_H), Image.BILINEAR), (0, 0))
    d = ImageDraw.Draw(canvas)
    if status:
        d.text((8, BAR_Y + 5), status, font=FONT_STATUS, fill=STATUS_COLOR)
    y = BAR_Y + 22
    for line in wrap(hint or "", FONT_HINT, W - 16, 2):
        d.text((8, y), line, font=FONT_HINT, fill=HINT_COLOR)
        y += 22
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=sc.QUALITY, subsampling=2)
    return buf.getvalue()
```

- [ ] **Step 4: 跑测试确认通过** → 4 PASS

---

### Task B3: 状态机 + 池调度

**Files:**
- Create: `bridge/screen_hub.py`
- Test: `bridge/test_screen_hub.py`

**Interfaces:**
- Produces:
  - `IDLE_POOL`（12 项）、`KEY_POOL`（6 项）
  - `AnimLibrary(frames_dir)`：`.get(name) -> list[PIL.Image]`、`.frame(name, t) -> PIL.Image`
  - `ScreenHub(lib, clock=time.monotonic, rng=random.Random())`：
    - `.tick()` 推进时间（由推流线程每帧调用）
    - `.frame_jpeg() -> bytes`
    - `.on_key()` / `.turn_begin()` / `.first_audio()` / `.turn_end(ok=True)` / `.turn_error()`
    - 属性 `.state`、`.anim`、`.hint`

- [ ] **Step 1: 写失败的测试（用假时钟，完全确定）**

```python
import unittest

import screen_hub as sh


class FakeClock(object):
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class HubTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.hub = sh.ScreenHub(lib=None, clock=self.clock, rng=sh.random.Random(7))

    def test_starts_breathing(self):
        self.assertEqual(self.hub.anim, "待机呼吸休闲")
        self.assertEqual(self.hub.state, "idle")

    def test_turn_states(self):
        self.hub.turn_begin()
        self.assertEqual(self.hub.state, "thinking")
        self.hub.first_audio()
        self.assertEqual(self.hub.state, "playing")
        self.hub.turn_end()
        self.assertEqual(self.hub.state, "idle")

    def test_key_always_switches_to_recording(self):
        self.hub.on_key()
        self.assertEqual(self.hub.state, "recording")
        self.assertIn("听", self.hub.hint)

    def test_idle_insert_only_after_enter_window(self):
        self.clock.t = 30
        self.hub.tick()
        self.assertFalse(self.hub.idle_active)
        self.clock.t = 130
        self.hub.tick()
        self.assertTrue(self.hub.idle_active)

    def test_insert_repeats_every_3_to_5_minutes(self):
        self.clock.t = 130
        self.hub.tick()
        first = self.hub.anim
        self.assertIn(first, sh.IDLE_POOL)
        self.clock.t = 130 + 181
        self.hub.tick()
        self.assertIn(self.hub.anim, sh.IDLE_POOL)

    def test_key_after_idle_wins_over_reaction_possibly(self):
        self.clock.t = 200
        self.hub.tick()
        played = []
        for _ in range(50):
            hub = sh.ScreenHub(lib=None, clock=self.clock, rng=sh.random.Random(_))
            self.clock.t = 200
            hub.tick()
            hub.on_key()
            played.append(hub.anim in sh.KEY_POOL)
        ratio = sum(played) / float(len(played))
        self.assertGreater(ratio, 0.4)
        self.assertLess(ratio, 0.8)

    def test_key_is_never_a_prefix_of_recording_delay(self):
        self.clock.t = 200
        self.hub.tick()
        self.hub.on_key()
        self.assertEqual(self.hub.state, "recording")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

```python
"""屏幕状态机：什么时候显示哪段动画、文字条写什么。

所有时间都走注入的 clock，测试里用假时钟，行为完全可复现。
"""
import random
import time

import screen_config as sc

IDLE_POOL = [
    "原地小憩沉眠", "超大伸懒腰", "照镜子", "撸猫", "打瞌睡被惊醒", "东张西望",
    "摇扇纳凉", "原地蹲下玩玩具汽车", "大口吃零食", "碎碎念-发呆碎碎念",
    "鲸鱼吐泡泡特效", "轻快摇摆舞",
]
KEY_POOL = ["被吓一跳", "点击回应-害羞惊讶", "点击回应-元气挥手",
            "点击回应-开心跃动", "点击回应-挠痒咯咯笑", "点击回应-傲娇生气"]
BREATHING = "待机呼吸休闲"
THINKING = "工作状态-思考冒泡"
PLAYING = "悠闲哼歌"
LISTENING = "东张西望"
ERROR = "工作状态-垂头叹气冒汗"


class AnimLibrary:
    """按需加载动画帧；LRU 只留 3 段（每段 ≤60 帧 320×180 ≈ 10 MB，估算）。"""

    def __init__(self, frames_dir):
        import json
        import os
        from collections import OrderedDict
        from PIL import Image
        self.frames_dir = frames_dir
        self.Image = Image
        self.OrderedDict = OrderedDict
        with open(os.path.join(frames_dir, "index.json"), encoding="utf-8") as fh:
            self.index = json.load(fh)
        self.cache = OrderedDict()

    def get(self, name):
        if name in self.cache:
            self.cache.move_to_end(name)
            return self.cache[name]
        import os
        meta = self.index.get(name) or self.index[BREATHING]
        frames = []
        for i in range(1, meta["frames"] + 1):
            path = os.path.join(self.frames_dir, name, "%04d.jpg" % i)
            frames.append(self.Image.open(path).convert("RGB").copy())
        self.cache[name] = frames
        while len(self.cache) > 3:
            self.cache.popitem(last=False)
        return frames

    def frame(self, name, t):
        frames = self.get(name)
        if not frames:
            return None
        meta = self.index.get(name) or self.index[BREATHING]
        return frames[int(t * meta["fps"]) % len(frames)]


class ScreenHub:
    def __init__(self, lib, clock=time.monotonic, rng=None):
        self.lib = lib
        self.clock = clock
        self.rng = rng or random.Random()
        self.state = "idle"
        self.anim = BREATHING
        self.hint = "按一下就说话"
        self.status = ""
        self._anim_started = clock()
        self._loop = True
        self._anim_duration = None
        self.idle_active = False
        self._idle_since = clock()
        self._next_insert = clock()
        self._recent = []
        self._enter_s = None

    # ---- 内部 ----
    def _set_anim(self, name, loop=True, duration=None):
        self.anim = name
        self._anim_started = self.clock()
        self._loop = loop
        self._anim_duration = duration

    def _enter_window(self):
        if self._enter_s is None:
            self._enter_s = self.rng.uniform(*sc.IDLE_ENTER_S)
        return self._enter_s

    def _insert_idle(self, now):
        lo, hi = sc.IDLE_EVERY_S
        self._next_insert = now + self.rng.uniform(lo, hi)
        fresh = [n for n in IDLE_POOL
                 if n not in [x for x, at in self._recent if now - at < sc.REPEAT_WINDOW_S]]
        name = self.rng.choice(fresh or IDLE_POOL)
        self._recent.append((name, now))
        self._set_anim(name, loop=False, duration=self.rng.uniform(2.5, 5.0))

    # ---- 对外 ----
    def tick(self):
        now = self.clock()
        if self.state == "idle":
            if not self.idle_active and now - self._idle_since >= self._enter_window():
                self.idle_active = True
                self._next_insert = now
            if self.idle_active and now >= self._next_insert:
                self._insert_idle(now)
        if not self._loop and self._anim_duration is not None:
            if now - self._anim_started >= self._anim_duration:
                self._set_anim(BREATHING)

    def frame_jpeg(self):
        import screen_compose
        img = self.lib.frame(self.anim, self.clock() - self._anim_started) if self.lib else None
        return screen_compose.compose(img, self.status, self.hint)

    def on_key(self):
        now = self.clock()
        idle_long = self.state == "idle" and (now - self._idle_since) >= self._enter_window()
        self.state = "recording"
        self.hint = "正在听…"
        self.idle_active = False
        if idle_long and self.rng.random() < sc.KEY_REACTION_PROB:
            lo, hi = sc.REACTION_S
            self._set_anim(self.rng.choice(KEY_POOL), loop=False,
                           duration=self.rng.uniform(lo, hi))
        else:
            self._set_anim(LISTENING)

    def turn_begin(self):
        self.state = "thinking"
        self.hint = "思考中…"
        self._set_anim(THINKING)

    def first_audio(self):
        if self.state == "playing":
            return
        self.state = "playing"
        self.hint = "正在播放"
        self._set_anim(PLAYING)

    def turn_end(self, ok=True):
        if not ok:
            return self.turn_error()
        self.state = "idle"
        self.hint = "按一下就说话"
        self.idle_active = False
        self._idle_since = self.clock()
        self._set_anim(BREATHING)

    def turn_error(self):
        self.state = "error"
        self.hint = "连不上了，我在"
        self._set_anim(ERROR, loop=True)
```

- [ ] **Step 4: 跑测试确认通过** → 7 PASS

- [ ] **Step 5: 反着验一次（自检测试真的会红）**

把 `KEY_REACTION_PROB` 临时改成 `0.0` 跑 `test_key_after_idle_wins_over_reaction_possibly`，Expected: FAIL（ratio≈0）；改回。

---

### Task B4: MJPEG 推流与按键路由

**Files:**
- Create: `bridge/screen_http.py`
- Test: `bridge/test_screen_http.py`

**Interfaces:**
- Consumes: `ScreenHub`
- Produces:
  - `Streamer(hub, fps)`：后台线程 `.start()` / `.stop()`；`.subscribe(handler_conn) -> queue.Queue` / `.unsubscribe(q)`
  - `write_stream(handler, streamer)`：把 multipart 写进现有 `BaseHTTPRequestHandler`
  - `handle_key(handler, hub)`：写 JSON

- [ ] **Step 1: 写失败的测试（真起一个 HTTP 服务）**

```python
import threading
import time
import unittest

import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

    def test_stream_delivers_frames(self):
        r = requests.get("http://127.0.0.1:%d/screen" % self.port, stream=True, timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertIn("multipart/x-mixed-replace", r.headers["Content-Type"])
        buf, frames = b"", 0
        for chunk in r.iter_content(4096):
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
                frames += 1
                buf = rest[n + 2:]
            if frames >= 3:
                break
        r.close()
        self.assertGreaterEqual(frames, 3)

    def test_key_endpoint_sets_recording(self):
        r = requests.post("http://127.0.0.1:%d/screen/key" % self.port, timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "recording")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

```python
"""MJPEG 推流 + 按键事件路由。

协议（设备端照这个解析）：
  200
  Content-Type: multipart/x-mixed-replace; boundary=frame
  Connection: close
  <body 是 close-delimited 的裸流>
  --frame\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\nX-State: idle\r\n\r\n<N 字节 JPEG>\r\n
用「连接关闭即结束」而不是 chunked，设备端少一层解析。
"""
import json
import queue
import threading
import time

import screen_config as sc
import screen_hub


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
                self.broadcast(self.hub.frame_jpeg())
            except Exception as exc:            # 一帧失败不能杀掉推流
                print("  WARN screen frame: %s: %s" % (type(exc).__name__, exc))
            self._stop.wait(max(0.0, period - (time.monotonic() - t0)))

    def start(self):
        self._thread = threading.Thread(target=self._run, name="screen-stream", daemon=True)
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
    blob = json.dumps({"state": hub.state, "anim": hub.anim}).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(blob)))
    handler.end_headers()
    handler.wfile.write(blob)


def make_test_hub():
    return screen_hub.ScreenHub(lib=None)
```

- [ ] **Step 4: 跑测试确认通过** → 2 PASS

- [ ] **Step 5: 真机前的手工目检**

```bash
cd C:/Users/DELL/voice-ai && SCREEN_ENABLED=1 ./.venv/Scripts/python.exe - <<'PY'
import screen_http, screen_hub, time, os
hub = screen_hub.ScreenHub(screen_hub.AnimLibrary(os.path.join("bridge","screen_frames")))
s = screen_http.Streamer(hub, fps=10); s.start(); q = s.subscribe()
for i in range(3):
    blob = q.get(timeout=5)
    open(r"C:\Users\DELL\AppData\Local\Temp\frame_%d.jpg" % i, "wb").write(blob)
print("frames ok", [os.path.getsize(r"C:\Users\DELL\AppData\Local\Temp\frame_%d.jpg" % i) for i in range(3)])
PY
```
Expected: 3 个文件，每个 8,000–20,000 B（估算）；用看图软件打开确认是「呼吸动画 + 底部两行字」。

---

### Task B5: 接进 server.py

**Files:**
- Modify: `bridge/server.py`（顶部 import、`do_GET`、`do_POST`、`serve()` 的启动/关闭、`/turn` 三个钩子）
- Test: `bridge/test_screen_server_hooks.py`

**Interfaces:**
- Consumes: `screen_config`、`screen_hub`、`screen_http`
- Produces: `GET /screen`、`POST /screen/key`；`server.build_screen()` 返回 `(hub, streamer)` 或 `(None, None)`

- [ ] **Step 1: 写失败的测试（不启动真 ASR/TTS，只测构建与关闭）**

```python
import os
import unittest


class HookTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        os.environ["SCREEN_ENABLED"] = "0"
        import importlib
        import screen_config
        importlib.reload(screen_config)
        import server
        importlib.reload(server)
        hub, streamer = server.build_screen()
        self.assertIsNone(hub)
        self.assertIsNone(streamer)

    def test_enabled_builds_hub(self):
        os.environ["SCREEN_ENABLED"] = "1"
        import importlib
        import screen_config
        importlib.reload(screen_config)
        import server
        importlib.reload(server)
        hub, streamer = server.build_screen()
        self.assertIsNotNone(hub)
        self.assertIsNotNone(streamer)
        streamer.stop()
        os.environ["SCREEN_ENABLED"] = "0"


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 server.py**

顶部加：

```python
import screen_config
import screen_hub
import screen_http
```

模块级：

```python
def build_screen():
    if not screen_config.SCREEN_ENABLED:
        print("screen: disabled (SCREEN_ENABLED=0)")
        return None, None
    lib = screen_hub.AnimLibrary(screen_config.FRAMES_DIR)
    hub = screen_hub.ScreenHub(lib)
    streamer = screen_http.Streamer(hub)
    streamer.start()
    print("screen: enabled frames=%s fps=%s" % (len(lib.index), screen_config.FPS))
    return hub, streamer
```

`Handler` 增加类属性 `screen_hub = None`、`screen_streamer = None`，并新增：

```python
    def do_GET(self):
        if self._route().startswith("/screen"):
            if not self._authorized():
                return self._reply(401, b'{"error":"bad auth"}', close=True)
            if self.screen_streamer is None:
                return self._reply(503, b'{"error":"screen disabled"}', close=True)
            return screen_http.write_stream(self, self.screen_streamer)
        self.send_error(404)
```

`do_POST` 的路由集合改成：

```python
        if route not in ("/turn", "/stop", "/brain", "/screen/key") or not self._authorized():
            return self._reply(401, b'{"error":"bad auth"}', close=True)
```

并在读完 body 之后、`/brain` 之前插入：

```python
        if route == "/screen/key":
            if self.screen_hub is None:
                return self._reply(503, b'{"error":"screen disabled"}')
            return screen_http.handle_key(self, self.screen_hub)
```

`/turn` 的三个钩子（都在 `try` 里，屏幕异常不影响语音）：

```python
        hub = self.screen_hub
        if hub is not None:
            hub.turn_begin()                       # 接在 bridge.begin 之后
```
```python
            for block in turn.pcm_chunks():
                if hub is not None and not hub_playing:
                    hub.first_audio(); hub_playing = True
                self._chunk(block)
```
```python
        finally:
            if hub is not None:
                hub.turn_end(ok=not turn.error)
```

`serve()` 里：

```python
    hub, streamer = build_screen()
    Handler.screen_hub, Handler.screen_streamer = hub, streamer
    ...
    finally:
        if streamer is not None:
            streamer.stop()
        stop_memory(bridge)
```

- [ ] **Step 4: 跑测试 + 起真 bridge 目检**

```bash
cd C:/Users/DELL/voice-ai && ./.venv/Scripts/python.exe -m unittest bridge.test_screen_server_hooks -v
cd C:/Users/DELL/voice-ai && SCREEN_ENABLED=1 ./.venv/Scripts/python.exe bridge/server.py --serve
# 另开一个窗口：
curl -s -m 3 -D - -o /dev/null http://127.0.0.1:8080/screen
```
Expected: `HTTP/1.1 200`、`Content-Type: multipart/x-mixed-replace; boundary=frame`

- [ ] **Step 5: 确认语音没被拖慢（回归）**

用它现有的 `test_e2e.py` 或一次真机对话：串口 `FIRST_AUDIO_MS` 与未开屏时同量级（实测基线 2.2–4.2 s）。若明显变差，先把 `SCREEN_FPS` 降到 6 再测。

---

### Task C1: 固件——ST7789 最小驱动

**Files:**
- Create: `firmware/voice_terminal/display_st7789.h`
- Create: `firmware/voice_terminal/display_st7789.cpp`
- Modify: `firmware/voice_terminal/config.h`（新增屏幕常量）
- Modify: `firmware/voice_terminal/voice_terminal.ino`（setup 里 `lcdInit(); lcdFill(0);`）

**Interfaces:**
- Produces: `void lcdInit()`、`void lcdFill(uint16_t color)`、`void lcdBlitFrame(const uint16_t* fb)`（fb 为 320×240 big-endian RGB565）

- [ ] **Step 1: 装 TJpg_Decoder（唯一新增第三方库）**

```bash
cd /tmp && NO_PROXY="*" curl -sSL -o tjpg.zip https://codeload.github.com/Bodmer/TJpg_Decoder/zip/refs/heads/master
cd "D:/Arduino15/user" && mkdir -p libraries && cd libraries && unzip -q -o /tmp/tjpg.zip && mv TJpg_Decoder-master TJpg_Decoder && ls TJpg_Decoder/src
```
Expected: `TJpg_Decoder.cpp TJpg_Decoder.h User_Config.h tjpgd.c tjpgd.h tjpgdcnf.h`
（`arduino-cli lib install` 走 127.0.0.1:7890 代理会失败——本机实测；直接下载解压到 user 库目录等效。）

- [ ] **Step 2: config.h 加常量**

```cpp
// ===== 屏幕（ST7789 240x320，横屏用 320x240）=====
static constexpr int PIN_LCD_SCLK = 12;
static constexpr int PIN_LCD_MOSI = 11;
static constexpr int PIN_LCD_CS   = 10;
static constexpr int PIN_LCD_DC   = 13;
static constexpr int PIN_LCD_RST  = 14;
static constexpr uint32_t LCD_SPI_HZ = 40000000;
static constexpr uint16_t LCD_W = 320;
static constexpr uint16_t LCD_H = 240;
static constexpr size_t   SCREEN_JPEG_MAX = 64 * 1024;   // PSRAM 收帧缓冲
static constexpr uint32_t SCREEN_RECONNECT_MS = 2000;
static constexpr uint32_t SCREEN_IDLE_TIMEOUT_MS = 4000; // 这么久没帧就当电脑不在
```

- [ ] **Step 3: 写驱动**

`display_st7789.h`：

```cpp
#pragma once
#include <Arduino.h>

void lcdInit();
void lcdFill(uint16_t color);
void lcdBlitFrame(const uint16_t* fb);   // 320x240, RGB565 big-endian
```

`display_st7789.cpp`：

```cpp
#include "display_st7789.h"
#include "config.h"
#include <SPI.h>

static SPIClass s_spi(FSPI);

static void cmd(uint8_t c) {
  digitalWrite(PIN_LCD_DC, LOW);
  digitalWrite(PIN_LCD_CS, LOW);
  s_spi.write(c);
  digitalWrite(PIN_LCD_CS, HIGH);
}

static void data(const uint8_t* d, size_t n) {
  digitalWrite(PIN_LCD_DC, HIGH);
  digitalWrite(PIN_LCD_CS, LOW);
  s_spi.writeBytes(d, n);
  digitalWrite(PIN_LCD_CS, HIGH);
}

static void cmdData(uint8_t c, const uint8_t* d, size_t n) {
  cmd(c);
  data(d, n);
}

static void setWindow(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1) {
  uint8_t buf[4];
  buf[0] = x0 >> 8; buf[1] = x0 & 0xFF; buf[2] = x1 >> 8; buf[3] = x1 & 0xFF;
  cmdData(0x2A, buf, 4);
  buf[0] = y0 >> 8; buf[1] = y0 & 0xFF; buf[2] = y1 >> 8; buf[3] = y1 & 0xFF;
  cmdData(0x2B, buf, 4);
  cmd(0x2C);
}

void lcdInit() {
  pinMode(PIN_LCD_CS, OUTPUT);
  pinMode(PIN_LCD_DC, OUTPUT);
  pinMode(PIN_LCD_RST, OUTPUT);
  digitalWrite(PIN_LCD_CS, HIGH);

  digitalWrite(PIN_LCD_RST, HIGH); delay(10);
  digitalWrite(PIN_LCD_RST, LOW);  delay(20);
  digitalWrite(PIN_LCD_RST, HIGH); delay(120);

  s_spi.begin(PIN_LCD_SCLK, -1, PIN_LCD_MOSI, -1);
  s_spi.beginTransaction(SPISettings(LCD_SPI_HZ, MSBFIRST, SPI_MODE0));

  cmd(0x01); delay(120);              // SWRESET
  cmd(0x11); delay(120);              // SLPOUT
  uint8_t colmod = 0x55; cmdData(0x3A, &colmod, 1);   // RGB565
  uint8_t madctl = 0x60; cmdData(0x36, &madctl, 1);   // 横屏 320x240
  uint8_t porctrl[5] = {0x0C, 0x0C, 0x00, 0x33, 0x33}; cmdData(0xB2, porctrl, 5);
  uint8_t gate[1] = {0x35}; cmdData(0xB7, gate, 1);
  uint8_t vcom[1] = {0x19}; cmdData(0xBB, vcom, 1);
  uint8_t lcm[1] = {0x2C}; cmdData(0xC0, lcm, 1);
  uint8_t vdvv[1] = {0x01}; cmdData(0xC2, vdvv, 1);
  uint8_t vrh[1] = {0x12}; cmdData(0xC3, vrh, 1);
  uint8_t vdvv2[1] = {0x20}; cmdData(0xC4, vdvv2, 1);
  uint8_t fr[2] = {0x0F, 0x01}; cmdData(0xC6, fr, 2);
  uint8_t pw[1] = {0xA4}; cmdData(0xD0, pw, 1);
  uint8_t pv[2] = {0xA1, 0x00}; cmdData(0xE0, pv, 2);
  cmd(0x21);                          // INVON（这批模块常见，颜色反了就去掉这行）
  cmd(0x13); delay(10);               // NORON
  cmd(0x29); delay(20);               // DISPON
  lcdFill(0x0000);
}

void lcdFill(uint16_t color) {
  setWindow(0, 0, LCD_W - 1, LCD_H - 1);
  uint8_t line[LCD_W * 2];
  uint16_t be = (uint16_t)((color >> 8) | (color << 8));
  for (int i = 0; i < LCD_W; i++) { memcpy(line + i * 2, &be, 2); }
  digitalWrite(PIN_LCD_DC, HIGH);
  digitalWrite(PIN_LCD_CS, LOW);
  for (int y = 0; y < LCD_H; y++) s_spi.writeBytes(line, sizeof(line));
  digitalWrite(PIN_LCD_CS, HIGH);
}

void lcdBlitFrame(const uint16_t* fb) {
  setWindow(0, 0, LCD_W - 1, LCD_H - 1);
  digitalWrite(PIN_LCD_DC, HIGH);
  digitalWrite(PIN_LCD_CS, LOW);
  s_spi.writePixels(fb, (uint32_t)LCD_W * LCD_H);
  digitalWrite(PIN_LCD_CS, HIGH);
}
```

- [ ] **Step 4: 编译三模式**

```bash
mkdir -p firmware/voice_terminal/build
D:/arduino-cli/arduino-cli.exe --config-file D:/arduino-cli/arduino-cli.yaml compile \
  --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,PartitionScheme=default,CDCOnBoot=default" \
  --build-path firmware/voice_terminal/build \
  --build-property "build.extra_flags=-DAPP_MODE=2" firmware/voice_terminal
```
Expected: `0 error`；flash 用量比基线多约 10–15 KB（估算）

---

### Task C2: 固件——推流客户端 + 离线图 + 按键上报

**Files:**
- Create: `firmware/voice_terminal/screen_client.h`
- Create: `firmware/voice_terminal/screen_client.cpp`
- Modify: `firmware/voice_terminal/voice_terminal.ino`（`screenStart()`；按键处 `screenNotifyKey()`）

**Interfaces:**
- Consumes: `lcdInit`、`lcdBlitFrame`、`wifiBridgeHost()`、`BRIDGE_PORT`、`BRIDGE_TOKEN`、`SESSION_ID`
- Produces: `void screenStart()`、`void screenNotifyKey()`

- [ ] **Step 1: 写客户端**

`screen_client.h`：

```cpp
#pragma once
#include <Arduino.h>
void screenStart();
void screenNotifyKey();   // 非阻塞：只置标志，主循环里调用安全
```

`screen_client.cpp`：

```cpp
#include "screen_client.h"
#include "config.h"
#include "display_st7789.h"
#include "offline_jpg.h"
#include "wifi_setup.h"
#include <WiFi.h>
#include <TJpg_Decoder.h>

#if APP_MODE != APP_MODE_RECORD_ONLY
  #if __has_include("secrets.h")
    #include "secrets.h"
  #else
    #error "secrets.h not found: copy secrets.example.h to secrets.h and fill real values."
  #endif
#else
  #define BRIDGE_HOST "0.0.0.0"
  #define BRIDGE_PORT 0
  #define BRIDGE_TOKEN ""
  #define SESSION_ID "dev1"
#endif

static uint16_t* s_fb = nullptr;      // PSRAM 帧缓冲 320*240*2
static uint8_t*  s_jpg = nullptr;     // PSRAM 收帧缓冲
static volatile bool s_keyPending = false;
static uint16_t s_fbW = 0, s_fbH = 0; // 当前 JPEG 的尺寸

static bool blitCb(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* bmp) {
  for (uint16_t j = 0; j < h; j++) {
    uint16_t row = y + j, col = x;
    if (row >= LCD_H) break;
    for (uint16_t i = 0; i < w; i++, col++) {
      if (col >= LCD_W) break;
      s_fb[row * LCD_W + col] = bmp[j * w + i];
    }
  }
  return true;
}

static bool drawJpeg(const uint8_t* buf, size_t n) {
  static TJpg_Decoder jpg;
  jpg.setSwapBytes(true);          // ST7789 吃 big-endian
  jpg.setCallback(blitCb);
  memset(s_fb, 0, (size_t)LCD_W * LCD_H * 2);
  JRESULT r = jpg.drawJpg(0, 0, buf, (uint32_t)n);
  if (r != JDR_OK) {
    Serial.printf("screen_jpeg_err=%d\n", (int)r);
    return false;
  }
  lcdBlitFrame(s_fb);
  return true;
}

static void showOffline() {
  static bool shown = false;
  if (shown) return;
  drawJpeg(kOfflineJpg, kOfflineJpgLen);
  shown = true;
  Serial.println("screen=offline_image");
}

static bool readLine(WiFiClient& c, char* out, size_t cap, uint32_t ms) {
  size_t n = 0;
  uint32_t deadline = millis() + ms;
  while ((int32_t)(millis() - deadline) < 0) {
    while (c.available()) {
      char ch = (char)c.read();
      if (ch == '\r') continue;
      if (ch == '\n') { out[n] = 0; return true; }
      if (n + 1 < cap) out[n++] = ch;
    }
    if (!c.connected() && !c.available()) return false;
    delay(1);
  }
  return false;
}

static bool readExact(WiFiClient& c, uint8_t* dst, size_t n, uint32_t ms) {
  size_t got = 0;
  uint32_t deadline = millis() + ms;
  while (got < n && (int32_t)(millis() - deadline) < 0) {
    int avail = c.available();
    if (avail > 0) {
      size_t want = n - got;
      if ((size_t)avail < want) want = (size_t)avail;
      int r = c.read(dst + got, want);
      if (r <= 0) break;
      got += (size_t)r;
    } else {
      if (!c.connected()) break;
      delay(1);
    }
  }
  return got == n;
}

static void postKey() {
  WiFiClient c;
  if (!c.connect(wifiBridgeHost(), BRIDGE_PORT, 800)) return;
  c.printf("POST /screen/key HTTP/1.1\r\nHost: %s:%d\r\nX-Auth: %s\r\n"
           "Content-Length: 0\r\nConnection: close\r\n\r\n",
           wifiBridgeHost(), (int)BRIDGE_PORT, BRIDGE_TOKEN);
  char line[96];
  readLine(c, line, sizeof(line), 800);
  c.stop();
}

static void screenTask(void*) {
  char line[256];
  for (;;) {
    if (s_keyPending) { s_keyPending = false; postKey(); }

    WiFiClient c;
    c.setNoDelay(true);
    if (!c.connect(wifiBridgeHost(), BRIDGE_PORT, 3000)) {
      showOffline();
      vTaskDelay(pdMS_TO_TICKS(SCREEN_RECONNECT_MS));
      continue;
    }
    c.printf("GET /screen?session_id=%s HTTP/1.1\r\nHost: %s:%d\r\nX-Auth: %s\r\n"
             "Connection: close\r\n\r\n",
             SESSION_ID, wifiBridgeHost(), (int)BRIDGE_PORT, BRIDGE_TOKEN);

    if (!readLine(c, line, sizeof(line), SCREEN_IDLE_TIMEOUT_MS) || strncmp(line, "HTTP/1.1 200", 12) != 0) {
      Serial.printf("screen_http=%s\n", line);
      c.stop();
      showOffline();
      vTaskDelay(pdMS_TO_TICKS(SCREEN_RECONNECT_MS));
      continue;
    }
    while (readLine(c, line, sizeof(line), 2000)) { if (!line[0]) break; }  // 跳过响应头

    for (;;) {                                  // 一条连接里读很多帧
      if (s_keyPending) { s_keyPending = false; postKey(); }
      if (!readLine(c, line, sizeof(line), SCREEN_IDLE_TIMEOUT_MS)) break;
      if (strncmp(line, "--frame", 7) != 0) continue;
      size_t len = 0;
      while (readLine(c, line, sizeof(line), 2000)) {
        if (!line[0]) break;
        if (strncasecmp(line, "Content-Length:", 15) == 0) len = (size_t)atol(line + 15);
      }
      if (len == 0 || len > SCREEN_JPEG_MAX) { Serial.printf("screen_bad_len=%u\n", (unsigned)len); break; }
      if (!readExact(c, s_jpg, len, 3000)) { Serial.println("screen_short_frame"); break; }
      uint8_t crlf[2];
      readExact(c, crlf, 2, 500);
      drawJpeg(s_jpg, len);                     // 失败也继续，下一帧再来
    }
    c.stop();
    vTaskDelay(pdMS_TO_TICKS(500));
  }
}

void screenStart() {
  s_fb = (uint16_t*)heap_caps_malloc((size_t)LCD_W * LCD_H * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  s_jpg = (uint8_t*)heap_caps_malloc(SCREEN_JPEG_MAX, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!s_fb || !s_jpg) {
    Serial.println("screen=alloc_failed（屏幕任务不启动，语音不受影响）");
    return;
  }
  lcdInit();
  xTaskCreatePinnedToCore(screenTask, "screen", 8192, nullptr, 2, nullptr, 0);
  Serial.printf("screen=started fb=%uB jpg=%uB\n", (unsigned)(LCD_W * LCD_H * 2), (unsigned)SCREEN_JPEG_MAX);
}

void screenNotifyKey() { s_keyPending = true; }
```

- [ ] **Step 2: 接进主固件**

`voice_terminal.ino` 顶部：`#include "screen_client.h"`。
`setup()` 末尾（`wifiEnsure()` 之后）：

```cpp
#if APP_MODE != APP_MODE_RECORD_ONLY
  screenStart();
#endif
```

`loop()` 里 `ST_IDLE` 分支，紧跟在检测到按下之后（**在 playTone 之前，不能有 delay**）：

```cpp
      if (down && !s_waitRelease) {
        s_waitRelease = true;
        screenNotifyKey();            // 只置标志，联网在屏幕任务里做
        playTone(1000, 60, 4000);
```

- [ ] **Step 3: 编译三模式**

同 Task C1 Step 4。Expected: `0 error / 0 warning`；flash 增加约 40–45 KB（估算：TJpg 10–15 + 离线图 21.4 + 客户端 6）

- [ ] **Step 4: 记下真实数字（写进 README 和单页 §7）**

编译输出里的 `Sketch uses … bytes` 抄进表格，并标注「2026-10-28 实测（屏幕未上机）」。

---

### Task C3: 屏到货前的自检清单 + 文档同步

**Files:**
- Modify: `firmware/README.md`（§2.1 尺寸表加一行「+屏幕」、依赖说明加 TJpg_Decoder）
- Modify: `屏幕接线单页-2026-10-28.md`（§6 推流改为实际协议、§7 换成实测字节、§9 上机顺序加屏幕步骤）
- Modify: `bridge/README.md`（新增 `SCREEN_ENABLED` 等配置、`/screen` 与 `/screen/key`、帧库构建命令）

- [ ] **Step 1: 上机前用 curl 自检**

```bash
curl -s -D - -o NUL -m 2 http://127.0.0.1:8080/screen
```
Expected: `200` + `multipart/x-mixed-replace`。（未开机/未接线时这一步只验证电脑端，不验证屏。）

- [ ] **Step 2: 文档同步三处**（按上列文件逐条改，数字都用编译和 curl 的真实输出）

- [ ] **Step 3: 交给用户的验收步骤（屏到货后照做）**

1. 只接 3V3/GND/BL → 背光常亮、不烫
2. 加 6 根信号线 → 开机黑屏（`lcdFill(0)`）
3. 起 bridge（`SCREEN_ENABLED=1`）→ 看到呼吸动画 + 「按一下就说话」
4. 按一下 BOOT → 立刻「正在听…」（若刚空闲超过 1–2 分钟，60% 概率先播 1–2 s 惊讶动画，**同时已在录音**）
5. 电脑断网/关 bridge → 4 s 内切到「电脑关机了」离线图
6. 全程看串口 `underruns` 有没有因为屏幕变多

---

## Self-Review

- **Spec 覆盖**：§5 版式（B2 320×180+60）✓；§5A.1 情绪（**未覆盖**，见下）✓/✗；§5A.2 随机待机 3–5 分钟 + 池 12 + 30 分钟不重复（B3）✓；§5A.3 按键 60% + 不延迟录音（B4/C2）✓；§5.2 关机图（A2）✓；§6 推流（A1/B4/C2）✓；§7 flash（C2 Step 4 实测）✓。
- **未覆盖项**：情绪标签。它要改 LLM 提示词 + 验证模型是否稳定输出 `emotion` 字段，属于独立改动，且会动到现在跑通的对话链路（44 项未提交改动里就有 `bridge/llm.py`）。本轮不做，单独排期。
- **占位符扫描**：无 TBD/TODO；所有步骤都带可执行命令或代码。
- **类型一致性**：`ScreenHub` 的方法在 B3 定义、B4/B5 使用（`tick/frame_jpeg/on_key/turn_begin/first_audio/turn_end/turn_error`）一致；`compose(anim, status, hint) -> bytes` 在 B2 定义、B3 调用一致；固件 `lcdInit/lcdFill/lcdBlitFrame` 在 C1 定义、C2 调用一致；`kOfflineJpg/kOfflineJpgLen` 在 A2 生成、C2 引用一致。
- **风险**：`MADCTL=0x60` 与 `INVON` 是凭常见模块经验写的，颜色反了/方向不对时只改这两行；`TJpg_Decoder` 未在本机编译过，C1 第一步就要验证能编过。
