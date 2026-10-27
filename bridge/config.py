import os
import sys


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        sys.stderr.write("WARN: invalid %s=%s; using default %s\n"
                         % (name, ascii(raw), default))
        return default


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        sys.stderr.write("WARN: invalid %s=%s; using default %s\n"
                         % (name, ascii(raw), default))
        return default


VOICE_ROOT = r"C:\Users\DELL\voice-ai"

PY311 = os.path.join(VOICE_ROOT, "py311", "python.exe")
SYSTEM_PY = r"C:\Users\DELL\AppData\Local\Programs\Python\Python313\python.exe"

SENSEVOICE_DIR = os.path.join(VOICE_ROOT, "models", "SenseVoiceSmall")
COSYVOICE_DIR = os.path.join(VOICE_ROOT, "models", "CosyVoice-300M-SFT")
COSYVOICE_REPO = os.path.join(VOICE_ROOT, "CosyVoice")

SAMPLE_RATE = 16000
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = _env_int("BRIDGE_PORT", 8080)

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_MAX_TOKENS = _env_int("DEEPSEEK_MAX_TOKENS", 320)
DEEPSEEK_THINKING = os.environ.get("DEEPSEEK_THINKING", "disabled")
DEEPSEEK_KEY_FILE = os.environ.get(
    "DEEPSEEK_KEY_FILE", os.path.join(os.path.expanduser("~"), ".deepseek", "key")
)

BRIDGE_TOKEN_FILE = os.environ.get(
    "BRIDGE_TOKEN_FILE", os.path.join(os.path.expanduser("~"), ".deepseek", "bridge_token")
)

BRAINS_CONFIG_FILE = os.environ.get(
    "BRAINS_CONFIG_FILE", os.path.join(os.path.expanduser("~"), ".voice-ai", "brains.toml")
)
BRAIN_PROFILE = os.environ.get("BRAIN_PROFILE", "").strip() or None

# Emergency brain: only after every cloud profile fails before any text. It gets
# a functional prompt, never the companion persona or long-term memory.
LOCAL_PLUGIN_ENABLED = os.environ.get("LOCAL_PLUGIN_ENABLED", "0") == "1"
LOCAL_PLUGIN_BASE_URL = os.environ.get(
    "LOCAL_PLUGIN_BASE_URL", "http://127.0.0.1:11434/v1"
)
LOCAL_PLUGIN_MODEL = os.environ.get("LOCAL_PLUGIN_MODEL", "qwen3.5:2b-q4_K_M")
LOCAL_PLUGIN_MAX_TOKENS = _env_int("LOCAL_PLUGIN_MAX_TOKENS", 160)
LOCAL_PLUGIN_TIMEOUT_S = _env_float("LOCAL_PLUGIN_TIMEOUT_S", 15.0)

USAGE_LEDGER_FILE = os.environ.get(
    "USAGE_LEDGER_FILE", os.path.join(os.path.expanduser("~"), ".voice-ai", "usage.jsonl")
)

MEMORY_ENABLED = os.environ.get("MEMORY_ENABLED", "0") == "1"
MEMORY_DIR = os.environ.get(
    "MEMORY_DIR", os.path.join(os.path.expanduser("~"), ".voice-ai", "memory")
)
MEMORY_INPUT_BUDGET_TOKENS = _env_int("MEMORY_INPUT_BUDGET_TOKENS", 1050)
MEMORY_CURRENT_INPUT_MAX_CHARS = _env_int("MEMORY_CURRENT_INPUT_MAX_CHARS", 500)
# Stage 3 defaults to dry-run: extract and assemble, but do not write facts,
# episodes or sessions. Stage 4 flips this default only after acceptance.
MEMORY_DRY_RUN = os.environ.get("MEMORY_DRY_RUN", "1") == "1"
MEMORY_DEBUG = os.environ.get("MEMORY_DEBUG", "0") == "1"

# DeepSeek deepseek-flash peak rates, CNY per million tokens (2026-10-22).
# Off-peak is half; cache hits are cheaper. The ledger is a conservative estimate.
DEEPSEEK_PRICE_IN_CNY_PER_MTOK = _env_float(
    "DEEPSEEK_PRICE_IN_CNY_PER_MTOK", 2.0
)
DEEPSEEK_PRICE_OUT_CNY_PER_MTOK = _env_float(
    "DEEPSEEK_PRICE_OUT_CNY_PER_MTOK", 8.0
)

# Measured on the 3060: 125-206 ms per character once warm, so the splitter's
# 40-char ceiling costs ~5-6 s. A timeout must not fire mid-reply — falling
# back to a different voice is worse than waiting.
COSYVOICE_TIMEOUT = _env_float("COSYVOICE_TIMEOUT", 9.0)
COSYVOICE_SPEAKER = os.environ.get("COSYVOICE_SPEAKER", "中文女")
# Every sentence >= 18 chars, first one included. Each synthesis call costs
# ~0.6 s + ~0.11 s/char regardless of length, and the device starts playback
# holding only sentence one: if synthesising sentence two takes longer than
# sentence one's audio, the reply freezes at the start (measured 2026-10-27:
# 4.10 s audio vs 4.78 s synth -> 23 underruns = 0.74 s of silence).
# The second sentence therefore also breaks on weak marks at 20 chars, with a
# 26-char ceiling that forces a cut even when the LLM writes no punctuation:
# synth(s2) then stays under 3.5 s, i.e. under the 3.3-4.8 s audio of a natural
# 21-31 char first sentence.
SENTENCE_MIN_CHARS = _env_int("SENTENCE_MIN_CHARS", 18)
SENTENCE_FIRST_MIN_CHARS = _env_int("SENTENCE_FIRST_MIN_CHARS", 18)
SENTENCE_SECOND_MIN_CHARS = _env_int("SENTENCE_SECOND_MIN_CHARS", 20)
SENTENCE_SECOND_MAX_CHARS = _env_int("SENTENCE_SECOND_MAX_CHARS", 26)
# Measured 2026-10-27 on the 3060 laptop: autocast fp16 gave no speedup
# (RTF 0.91-0.93 vs 0.78-0.97 in fp32, i.e. inside the noise), so it stays off
# until something proves otherwise.
COSYVOICE_FP16 = bool(_env_int("COSYVOICE_FP16", 0))
EDGE_VOICE = os.environ.get("EDGE_VOICE", "zh-CN-XiaoxiaoNeural")

DEFAULT_PERSONA = "你是一只温柔的陪伴小机器，说话自然口语化，句子短，不用列表和markdown。"


def api_key():
    env = os.environ.get("DEEPSEEK_API_KEY")
    if env:
        return env.strip()
    if os.path.isfile(DEEPSEEK_KEY_FILE):
        with open(DEEPSEEK_KEY_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return None


def auth_token():
    env = os.environ.get("BRIDGE_TOKEN")
    if env:
        return env.strip()
    if os.path.isfile(BRIDGE_TOKEN_FILE):
        with open(BRIDGE_TOKEN_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return None


def force_utf8_output():
    # Windows gives a piped stdout the ANSI codepage (GBK here), so printing
    # recognized Chinese text raises UnicodeEncodeError and kills the turn.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
