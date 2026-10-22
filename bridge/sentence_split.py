import re

STRONG = "。！？!?；;\n"
WEAK = "，,、：:"
BREAKS = STRONG + WEAK

_MEANINGFUL = re.compile(r"[^\W_]")


def weight(text):
    """Count real characters, ignoring punctuation and whitespace."""
    return len(_MEANINGFUL.findall(text))


class SentenceSplitter:
    """Turn an LLM token stream into TTS-sized sentences.

    First sentence may break on a weak mark to get sound out sooner; the rest
    only break on strong marks so the prosody stays natural.
    """

    def __init__(self, min_chars=8, first_min_chars=5, hard_max_chars=40):
        self.min_chars = min_chars
        self.first_min_chars = first_min_chars
        self.hard_max_chars = hard_max_chars
        self._buf = ""
        self._emitted = False

    def feed(self, delta):
        if not delta:
            return []
        self._buf += delta
        out = []
        while True:
            head = self._cut_once()
            if head is None:
                break
            clean = _clean(head)
            if clean is not None:
                out.append(clean)
        return out

    def flush(self):
        tail, self._buf = self._buf, ""
        return _clean(tail)

    def _cut_once(self):
        limit = self.first_min_chars if not self._emitted else self.min_chars
        for i, ch in enumerate(self._buf):
            if ch not in BREAKS:
                continue
            if ch not in STRONG and self._emitted:
                continue
            head = self._buf[: i + 1]
            if weight(head) < limit:
                continue
            self._buf = self._buf[i + 1 :]
            self._emitted = True
            return head
        if weight(self._buf) >= self.hard_max_chars:
            head, self._buf = self._buf, ""
            self._emitted = True
            return head
        return None


def _clean(text):
    stripped = text.replace("\n", "").strip()
    return stripped if weight(stripped) else None
