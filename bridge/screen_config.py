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
RECORD_STUCK_S = _env_float("SCREEN_RECORD_STUCK_S", 20.0)   # 按键后没等到 /turn 就复位
FRAMES_DIR = os.path.join(_HERE, "screen_frames")
FONT_PATH = os.environ.get("SCREEN_FONT", r"C:\Windows\Fonts\msyh.ttc")
OFFLINE_JPG = os.path.join(_HERE, "screen_assets", "offline_320x240.jpg")
