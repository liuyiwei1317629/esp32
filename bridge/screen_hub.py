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

# 情绪 → 现有动画。只在开口那一刻选一次，播完自然回到「悠闲哼歌」。
EMOTION_ANIMS = {
    "excited": "工作状态-雀跃庆祝",
    "happy": "点击回应-开心跃动",
    "sad": "工作状态-垂头叹气冒汗",
    "angry": "点击回应-傲娇生气",
    "surprised": "被吓一跳",
    "shy": "点击回应-害羞惊讶",
}

# 桌宠原案的 16 类摆放（照 esp32肥鱼/素材分类/使用中/ 的目录名与顺序）。
# 同一段动画会在多个组里出现 —— 原案就是这么摆的，不是重复录入。
# 01 组的「关机图」是静态图、不在片库里，这里只列动画。
CATALOG = (
    ("01-关机或离线", ("原地小憩沉眠",)),
    ("02-常驻待机", ("待机呼吸休闲",)),
    ("03-录音中", ("东张西望", "碎碎念-对屏碎碎念", "工作状态-原地踱步张望")),
    ("04-等回复", ("工作状态-思考冒泡", "深度思考碎碎念", "写代码",
                 "工作状态-忙碌点按", "工作状态-清点归档", "原地专心玩魔方",
                 "轻快记录")),
    ("05-播放中", ("悠闲哼歌", "吹笛子", "小提琴演奏")),
    ("06-出错或连不上", ("工作状态-垂头叹气冒汗", "偷吃零食被抓住",
                       "余额-分文不剩", "余额-袋空如洗")),
    ("07-被打断", ("点击回应-傲娇生气", "被吓一跳")),
    ("08-随机待机池", (
        "东张西望", "原地小憩沉眠", "原地蹲下玩玩具汽车", "大口吃零食",
        "打瞌睡被惊醒", "摇扇纳凉", "撸猫", "照镜子", "碎碎念-发呆碎碎念",
        "超大伸懒腰", "轻快摇摆舞", "鲸鱼吐泡泡特效",
        "三球抛接", "下五子棋", "中秋赏月吃月饼", "优雅女仆舞",
        "余额-金袋叮当", "余额-钱袋如常", "余额-钱袋满溢",
        "写福字", "凭空生花", "动物环绕", "原地专心玩魔方", "原地左转奔跑",
        "原地敲击桌面互动", "原地漂浮踏步", "原地跳跃抓碎头顶物品",
        "原地重力下蹲压缩", "变鸽子", "吃Token", "吃冰淇淋融化", "吃午餐",
        "吃大闸蟹", "吃年糕", "吃早餐", "吃晚餐", "吃汤圆", "吃白饭",
        "吃粽子", "吃糖葫芦", "吃腊八粥", "吃西瓜", "吃重阳糕", "吃长寿面",
        "吃青团", "吃饺子", "吹气球", "吹笛子", "堆雪人", "女仆屈膝礼仪",
        "小幅度原地360度旋转展示", "小提琴演奏", "工作状态-原地踱步张望",
        "工作状态-忙碌点按", "工作状态-清点归档", "扑克魔术", "抽陀螺",
        "拆礼物", "插茱萸赏菊", "收红包", "放孔明灯", "放河灯", "放烟花",
        "放风筝", "整体换装试色", "是啊，吃什么", "晨间刷牙", "涮火锅",
        "深度思考碎碎念", "玩水枪", "玩游戏气急败坏", "用鲸鱼尾巴拍打地面",
        "碎碎念-对屏碎碎念", "碎碎念-擦桌碎碎念", "穿针乞巧", "舞狮头",
        "荡秋千", "萌化小幽灵", "蓝鲸现世", "蝴蝶蜜蜂环绕头顶开花", "螃蟹走路",
        "被落叶淹没", "被鼠标拖拽悬空反馈", "装点圣诞树", "讨糖南瓜灯",
        "踢毽子", "轻快记录", "骑木马")),
    ("09-表情-开心", ("工作状态-雀跃庆祝", "点击回应-开心跃动", "轻快摇摆舞",
                    "收红包", "拆礼物", "放烟花", "舞狮头", "三球抛接",
                    "玩水枪", "踢毽子", "荡秋千")),
    ("10-表情-温柔", ("可爱宅舞", "摇扇纳凉", "撸猫", "优雅女仆舞",
                    "女仆屈膝礼仪", "插茱萸赏菊")),
    ("11-表情-好奇", ("东张西望", "照镜子", "碎碎念-发呆碎碎念",
                    "是啊，吃什么", "原地敲击桌面互动")),
    ("12-表情-困", ("原地小憩沉眠", "哈欠连天", "打瞌睡被惊醒")),
    ("13-表情-难过", ("工作状态-垂头叹气冒汗", "余额-数金皱眉", "被落叶淹没")),
    ("14-表情-生气", ("点击回应-傲娇生气", "被吓一跳", "玩游戏气急败坏",
                    "用鲸鱼尾巴拍打地面")),
    ("15-表情-中性", ("待机呼吸休闲", "原地重力下蹲压缩",
                    "小幅度原地360度旋转展示", "整体换装试色", "原地漂浮踏步")),
    ("16-按键反应", ("点击回应-傲娇生气", "点击回应-元气挥手", "点击回应-害羞惊讶",
                    "点击回应-开心跃动", "点击回应-挠痒咯咯笑", "被吓一跳",
                    "被鼠标拖拽悬空反馈", "原地敲击桌面互动")),
)
CATALOG_ANIMS = frozenset(name for _, anims in CATALOG for name in anims)

# 调试台按钮上显示的中文名；顺序跟 emotion.EMOTIONS 一致
EMOTION_LABELS = {
    "excited": "兴奋",
    "happy": "开心",
    "sad": "伤心",
    "angry": "生气",
    "surprised": "惊讶",
    "shy": "害羞",
}

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

    def first_audio(self, hint=None, emotion=None):
        if self.state == "playing" and hint is None and emotion is None:
            return
        self.state = "playing"
        self.hint = hint or HINT_PLAY
        self._set_anim(EMOTION_ANIMS.get(emotion, PLAYING))

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

    def show_emotion(self, emotion):
        """调试台手动换表情：一直停在挑的表情上，直到按键或回「自动」。"""
        if emotion not in EMOTION_LABELS:
            self.show_auto()
            return False
        self.state = "preview"
        self.hint = EMOTION_LABELS[emotion]
        self.idle_active = False
        self._set_anim(EMOTION_ANIMS.get(emotion, PLAYING))
        return True

    def show_anim(self, name):
        """手动播任意动作；只认原案目录里的名字。"""
        if name not in CATALOG_ANIMS:
            return False
        if self.lib is not None and name not in self.lib.index:
            return False
        self.state = "preview"
        self.hint = name
        self.idle_active = False
        self._set_anim(name)
        return True

    def catalog(self):
        """给调试台的分组清单；片库里没有的动画顺手滤掉。"""
        groups = []
        for title, anims in CATALOG:
            names = [n for n in anims
                     if self.lib is None or n in self.lib.index]
            if names:
                groups.append({"title": title, "anims": names})
        return groups

    def show_auto(self):
        self.state = "idle"
        self.hint = HINT_IDLE
        self.idle_active = False
        self._idle_since = self.clock()
        self._set_anim(BREATHING)
