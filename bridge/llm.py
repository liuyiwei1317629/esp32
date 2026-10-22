import json
import queue
import socket
import threading
import time

import requests

import brains
import config


DeepSeekError = brains.BrainError


def consume(lines, on_content, abort=None, clock=time.perf_counter):
    """Parse SSE byte lines, forward only delta.content.

    Returns per-stage timings plus the last non-empty usage object seen, which
    covers both documented placements (trailing empty-choices chunk, or attached
    to the final content chunk).
    """
    stats = {
        "first_token_seconds": None,
        "total_seconds": 0.0,
        "chunks": 0,
        "content_chars": 0,
        "reasoning_chars": 0,
        "finish_reason": None,
        "usage": None,
        "aborted": False,
        "done": False,
    }
    t0 = clock()
    for raw in lines:
        if abort is not None and abort.is_set():
            stats["aborted"] = True
            break
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            stats["done"] = True
            break
        try:
            evt = json.loads(data)
        except ValueError:
            continue
        usage = evt.get("usage")
        if usage:
            stats["usage"] = usage
        for choice in evt.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if reasoning:
                stats["reasoning_chars"] += len(reasoning)
            if choice.get("finish_reason"):
                stats["finish_reason"] = choice["finish_reason"]
            content = delta.get("content")
            if not content:
                continue
            if stats["first_token_seconds"] is None:
                stats["first_token_seconds"] = clock() - t0
            stats["chunks"] += 1
            stats["content_chars"] += len(content)
            on_content(content)
    stats["total_seconds"] = clock() - t0
    return stats


class _LinePump:
    """Read a blocking requests stream on a thread so the caller can time out."""

    def __init__(self, resp, abort):
        self._resp = resp
        self._abort = abort
        self._q = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="brain-lines")
        self._thread.start()

    def _run(self):
        try:
            for line in self._resp.iter_lines():
                if self._abort is not None and self._abort.is_set():
                    break
                self._q.put(("line", line))
        except Exception as exc:
            if not self._closed:
                self._q.put(("error", exc))
        finally:
            try:
                self._resp.close()
            except Exception:
                pass
            self._q.put(("end", None))

    def get(self, timeout):
        return self._q.get(timeout=timeout)

    def close(self):
        # Closing from another thread while iter_lines() is blocked can wait
        # for the socket read timeout; shut the socket down and let the reader
        # thread close the response.
        self._closed = True
        if not self._thread.is_alive():
            return
        try:
            sock = self._resp.raw._fp.fp.raw._sock
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            threading.Thread(target=self._resp.close, daemon=True).start()


def _deadline_lines(pump, deadline, seen_content, abort, clock=time.perf_counter):
    while True:
        if abort is not None and abort.is_set():
            return
        if seen_content["value"]:
            timeout = 0.2
        else:
            timeout = deadline - clock()
            if timeout <= 0:
                raise brains.BrainError("first token timeout", retryable=True)
            timeout = min(timeout, 0.2)
        try:
            kind, payload = pump.get(timeout)
        except queue.Empty:
            continue
        if kind == "line":
            yield payload
        elif kind == "error":
            raise payload
        else:
            return


def _auth_headers(profile):
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    key = profile.key()
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def stream_profile(profile, messages, on_content, abort=None):
    payload = {
        "model": profile.model,
        "messages": messages,
        "stream": True,
        "max_tokens": profile.max_tokens,
        "stream_options": {"include_usage": True},
    }
    payload.update(profile.extra_body or {})
    url = profile.base_url.rstrip("/") + "/chat/completions"
    started = time.perf_counter()
    first = profile.first_token_timeout_s
    try:
        # The first-token budget covers connect + response headers too: a
        # provider that stalls before headers must not stretch the deadline.
        resp = requests.post(url, json=payload, headers=_auth_headers(profile),
                             stream=True, timeout=(min(5.0, first), first))
    except requests.Timeout as exc:
        raise brains.BrainError("first token timeout", retryable=True) from exc
    except requests.RequestException as exc:
        raise brains.BrainError("connect %s: %s" % (profile.name, exc),
                                 retryable=True) from exc

    if resp.status_code != 200:
        detail = resp.text[:400]
        resp.close()
        retryable = resp.status_code >= 500 or resp.status_code in (408, 425)
        raise brains.BrainError(
            "HTTP %d from %s: %s" % (resp.status_code, profile.name, detail),
            retryable=retryable, status=resp.status_code)

    seen = {"value": False}

    def forward(text):
        seen["value"] = True
        on_content(text)

    try:
        # Headers are in; resume the normal per-read timeout for the stream.
        resp.raw._fp.fp.raw._sock.settimeout(profile.timeout_s)
    except Exception:
        pass

    pump = _LinePump(resp, abort)
    deadline = started + first
    try:
        stats = consume(_deadline_lines(pump, deadline, seen, abort), forward, abort)
    except brains.BrainError:
        raise
    except requests.RequestException as exc:
        raise brains.BrainError("stream %s: %s" % (profile.name, exc),
                                 retryable=True) from exc
    except Exception as exc:
        raise brains.BrainError("stream %s: %s" % (profile.name, exc),
                                 retryable=True) from exc
    finally:
        pump.close()
    if abort is not None and abort.is_set():
        stats["aborted"] = True
    return stats


def probe_profile(profile):
    payload = {
        "model": profile.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    payload.update(profile.extra_body or {})
    url = profile.base_url.rstrip("/") + "/chat/completions"
    try:
        with requests.post(url, json=payload, headers=_auth_headers(profile),
                           timeout=(5.0, min(profile.timeout_s, 10.0))) as resp:
            return resp.status_code == 200
    except requests.RequestException:
        return False


_router = None
_router_lock = threading.Lock()


def router():
    global _router
    with _router_lock:
        if _router is None:
            cfg = brains.load_config(config.BRAINS_CONFIG_FILE)
            _router = brains.BrainRouter(cfg, stream_profile, prober=probe_profile)
            if config.BRAIN_PROFILE:
                _router.set_active(config.BRAIN_PROFILE)
        return _router


def set_profile(name):
    return router().set_active(name)


def brain_status():
    return router().status()


def profile_prices(name):
    prof = router().cfg.profiles.get(name)
    if prof is None:
        return None, None
    return prof.price_in_cny_per_mtok, prof.price_out_cny_per_mtok


def stream_reply(messages, on_content, abort=None):
    return router().stream_reply(messages, on_content, abort)
