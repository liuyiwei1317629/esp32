"""屏幕状态机：什么时候显示哪段动画、文字条写什么。

所有时间都走注入的 clock，测试里用假时钟，行为完全可复现。
"""
import json
import os
import random
import time
from collections import OrderedDict

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

HINT_IDLE = "按一下就说话"
HINT_LISTEN = "正在听…"
HINT_THINK = "思考中…"
HINT_PLAY = "正在播放"
HINT_ERROR = "连不上了，我在"


class AnimLibrary:
    """按需加载动画帧；LRU 只留 3 段（每段 ~100 帧 320×180，约 17 MB 内存）。"""

    def __init__(self, frames_dir, cache_size=3):
        from PIL import Image
        self.frames_dir = frames_dir
        self.Image = Image
        self.cache_size = cache_size
        with open(os.path.join(frames_dir, "index.json"), encoding="utf-8") as fh:
            self.index = json.load(fh)
        self.cache = OrderedDict()

    def get(self, name):
        if name in self.cache:
            self.cache.move_to_end(name)
            return self.cache[name]
        if name not in self.index:
            raise KeyError(name)
        meta = self.index[name]
        frames = []
        for i in range(1, meta["frames"] + 1):
            path = os.path.join(self.frames_dir, name, "%04d.jpg" % i)
            frames.append(self.Image.open(path).convert("RGB").copy())
        self.cache[name] = frames
        while len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return frames

    def frame(self, name, t):
        frames = self.get(name)
        if not frames:
            return None
        return frames[int(t * self.index[name]["fps"]) % len(frames)]


class ScreenHub:
    def __init__(self, lib, clock=time.monotonic, rng=None):
        self.lib = lib
        self.clock = clock
        self.rng = rng or random.Random()
        self.state = "idle"
        self.anim = BREATHING
        self.hint = HINT_IDLE
        self.idle_active = False
        self._anim_started = clock()
        self._loop = True
        self._anim_duration = None
        self._idle_since = clock()
        self._next_insert = clock()
        self._recording_since = clock()
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

    def _anim_expired(self, now):
        return (not self._loop and self._anim_duration is not None
                and now - self._anim_started >= self._anim_duration)

    def _settle(self):
        """一次性动画播完后回到该状态的常驻动画。"""
        if self.state == "recording":
            self._set_anim(LISTENING)
        elif self.state == "error":
            self._set_anim(ERROR)
        elif self.state == "thinking":
            self._set_anim(THINKING)
        elif self.state == "playing":
            self._set_anim(PLAYING)
        else:
            self._set_anim(BREATHING)

    # ---- 对外 ----
    def tick(self):
        now = self.clock()
        if self.state == "recording" and now - self._recording_since > sc.RECORD_STUCK_S:
            # 说完却一直没 POST /turn（或设备掉线）时别把屏卡在「正在听」。
            self.turn_end()
        if self.state == "idle":
            if not self.idle_active and now - self._idle_since >= self._enter_window():
                self.idle_active = True
                self._next_insert = now
            if self.idle_active and now >= self._next_insert:
                self._insert_idle(now)
        if self._anim_expired(now):
            self._settle()

    def frame_jpeg(self):
        import screen_compose
        img = None
        if self.lib is not None:
            img = self.lib.frame(self.anim, self.clock() - self._anim_started)
        return screen_compose.compose(img, self.hint)

    def on_key(self):
        now = self.clock()
        idle_long = self.state == "idle" and (now - self._idle_since) >= self._enter_window()
        self.state = "recording"
        self._recording_since = now
        self.hint = HINT_LISTEN
        self.idle_active = False
        if idle_long and self.rng.random() < sc.KEY_REACTION_PROB:
            lo, hi = sc.REACTION_S
            self._set_anim(self.rng.choice(KEY_POOL), loop=False,
                           duration=self.rng.uniform(lo, hi))
        else:
            self._set_anim(LISTENING)

    def turn_begin(self):
        self.state = "thinking"
        self.hint = HINT_THINK
        self._set_anim(THINKING)

    def first_audio(self, hint=None):
        if self.state == "playing" and hint is None:
            return
        self.state = "playing"
        self.hint = hint or HINT_PLAY
        self._set_anim(PLAYING)

    def turn_end(self, ok=True):
        if not ok:
            return self.turn_error()
        self.state = "idle"
        self.hint = HINT_IDLE
        self.idle_active = False
        self._idle_since = self.clock()
        self._set_anim(BREATHING)

    def turn_error(self):
        self.state = "error"
        self.hint = HINT_ERROR
        self._set_anim(ERROR)
