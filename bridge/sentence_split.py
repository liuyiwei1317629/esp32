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

    The first two sentences may break on a weak mark: the first to get sound
    out sooner, the second so its synthesis stays shorter than the first
    sentence's audio (the device starts playback holding only sentence one).
    From the third sentence on, only strong marks are used.
    """

    def __init__(self, min_chars=8, first_min_chars=5, second_min_chars=None,
                 second_max_chars=None, hard_max_chars=40):
        self.min_chars = min_chars
        self.first_min_chars = first_min_chars
        self.second_min_chars = (
            min_chars if second_min_chars is None else second_min_chars)
        self.second_max_chars = (
            hard_max_chars if second_max_chars is None else second_max_chars)
        self.hard_max_chars = hard_max_chars
        self._buf = ""
        self._emitted = 0

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
        if self._emitted == 0:
            limit, weak_ok, hard = self.first_min_chars, True, self.hard_max_chars
        elif self._emitted == 1:
            limit, weak_ok = self.second_min_chars, True
            hard = self.second_max_chars
        else:
            limit, weak_ok, hard = self.min_chars, False, self.hard_max_chars
        for i, ch in enumerate(self._buf):
            if ch not in BREAKS:
                continue
            if ch not in STRONG and not weak_ok:
                continue
            head = self._buf[: i + 1]
            if weight(head) < limit:
                continue
            self._buf = self._buf[i + 1 :]
            self._emitted += 1
            return head
        if weight(self._buf) >= hard:
            head, self._buf = self._buf, ""
            self._emitted += 1
            return head
        return None


def _clean(text):
    stripped = text.replace("\n", "").strip()
    return stripped if weight(stripped) else None
