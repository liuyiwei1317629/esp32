import io
import os
import re
import tempfile
import time
import wave

import config

_TAG = re.compile(r"((?:<\|[^|]*\|>)+)")


class Asr:
    """SenseVoice loaded once and kept resident; ~0.25 s per utterance on CPU."""

    def __init__(self, device="cpu"):
        from funasr import AutoModel

        self.model = AutoModel(model=config.SENSEVOICE_DIR, device=device, disable_update=True)

    def transcribe_wav(self, blob):
        with wave.open(io.BytesIO(blob), "rb") as wav:
            return self._run(wav.readframes(wav.getnframes()), wav.getframerate())

    def transcribe_pcm(self, pcm16, rate=config.SAMPLE_RATE):
        return self._run(pcm16, rate)

    def _run(self, pcm16, rate):
        path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                path = tmp.name
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(pcm16)
            t0 = time.perf_counter()
            raw = self.model.generate(input=path, cache={}, language="auto", use_itn=True)[0]["text"]
            return _strip_tags(raw), round(time.perf_counter() - t0, 3)
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


def _strip_tags(text):
    m = _TAG.match(text)
    return text[m.end():].strip() if m else text.strip()
