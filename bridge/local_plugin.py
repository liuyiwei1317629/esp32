import requests

import config
import llm

LOCAL_SYSTEM_PROMPT = (
    "你现在是语音陪伴设备的本地应急模式，云端大脑暂时连不上。"
    "只做简单、短的回答，一到两句话，口语中文，不用列表和markdown。"
    "不清楚的事直接说不知道，不要编造，也不要假装记得之前聊过什么。"
)

FALLBACK_REPLY = "云端暂时连不上，我现在只能做简单的事。"

CONNECT_TIMEOUT_S = 3.0


class LocalPluginError(Exception):
    """The local emergency model failed; the caller speaks a fixed sentence."""


def _headers():
    return {"Content-Type": "application/json", "Accept": "text/event-stream"}


def stream_reply(user_text, on_content, abort=None):
    url = config.LOCAL_PLUGIN_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.LOCAL_PLUGIN_MODEL,
        "messages": [
            {"role": "system", "content": LOCAL_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "stream": True,
        "max_tokens": config.LOCAL_PLUGIN_MAX_TOKENS,
        "stream_options": {"include_usage": True},
        # Ollama's OpenAI endpoint ignores think=false and would emit only
        # delta.reasoning, leaving the reply empty; this is the working switch.
        "reasoning_effort": "none",
        # Low-frequency emergency path: unload right after the turn.
        "keep_alive": 0,
    }
    try:
        resp = requests.post(url, json=payload, headers=_headers(), stream=True,
                             timeout=(CONNECT_TIMEOUT_S,
                                      config.LOCAL_PLUGIN_TIMEOUT_S))
    except requests.Timeout as exc:
        raise LocalPluginError("local model timeout") from exc
    except requests.RequestException as exc:
        raise LocalPluginError("local model connect: %s" % exc) from exc

    try:
        if resp.status_code != 200:
            detail = (resp.text or "")[:400]
            raise LocalPluginError(
                "HTTP %d from local model: %s" % (resp.status_code, detail))
        try:
            stats = llm.consume(resp.iter_lines(), on_content, abort)
        except requests.RequestException as exc:
            raise LocalPluginError("local model stream: %s" % exc) from exc
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if abort is not None and abort.is_set():
        stats["aborted"] = True
    stats["profile"] = "local"
    stats["fallback_used"] = True
    if not stats["aborted"] and not stats["content_chars"]:
        raise LocalPluginError("local model returned no content")
    return stats
