"""把「动画帧 + 两行文字」合成成一张 320x240 JPEG。纯函数，方便单测。"""
import io

from PIL import Image, ImageDraw, ImageFont

import screen_config as sc

W, H = 320, 240
ANIM_H = 180
BAR_Y = ANIM_H
FONT_HINT = ImageFont.truetype(sc.FONT_PATH, 18)

HINT_COLOR = (236, 238, 245)
LINE_H = 21
FIRST_Y = 189          # 两行 18px 中文：墨迹 194–232，留 7 px 下边距


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


def compose(anim, hint):
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    if anim is not None:
        canvas.paste(anim.convert("RGB").resize((W, ANIM_H), Image.BILINEAR), (0, 0))
    d = ImageDraw.Draw(canvas)
    y = FIRST_Y
    for line in wrap(hint or "", FONT_HINT, W - 16, 2):
        d.text((8, y), line, font=FONT_HINT, fill=HINT_COLOR)
        y += LINE_H
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=sc.QUALITY, subsampling=2)
    return buf.getvalue()
