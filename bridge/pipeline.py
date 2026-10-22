import queue
import threading
import time

import commands
import config
import llm
import local_commands
import memory_context
from sentence_split import SentenceSplitter

STOP = None
CHUNK_BYTES = 4096


class Timers:
    def __init__(self):
        self.t0 = time.perf_counter()
        self.marks = {}

    def mark(self, name):
        self.marks[name] = round((time.perf_counter() - self.t0) * 1000)

    def summary(self):
        out = {"t_%s_ms" % k: v for k, v in self.marks.items()}
        if "first_byte" in self.marks:
            out["FIRST_AUDIO_MS"] = self.marks["first_byte"]
        return out


class Turn:
    """LLM stream -> sentence split -> TTS -> PCM queue, all three stages overlapped.

    While sentence k is being streamed out, k+1 is synthesizing and k+2 is still
    being generated. Any serialisation here shows up directly as dead air.
    """

    def __init__(self, bridge, gen, user_text, timers, session_id="anon"):
        self.bridge = bridge
        self.gen = gen
        self.user_text = user_text
        self.timers = timers
        self.session_id = session_id
        self.splitter = SentenceSplitter()
        self.sentence_q = queue.Queue()
        self.audio_q = queue.Queue(maxsize=16)
        self.abort = threading.Event()
        self.stats = {}
        self.error = None
        self.reply_text = ""
        self.completed = False
        self.selected_ids = []
        self.local_command = ""
        self._staged = False

    @property
    def stale(self):
        return self.abort.is_set() or self.bridge.superseded(self.gen)

    def start(self):
        threading.Thread(target=self._llm_stage, daemon=True).start()
        threading.Thread(target=self._tts_stage, daemon=True).start()

    def _llm_stage(self):
        try:
            if self._handle_local_command():
                return
            self._stream_llm()
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            self.sentence_q.put(STOP)

    def _handle_local_command(self):
        command = commands.parse_command(self.user_text)
        if command is None:
            return False
        self.local_command = command.kind
        reply = local_commands.reply_for(command)
        if not reply:
            return True
        self.reply_text = reply
        for sentence in self.splitter.feed(reply):
            self.sentence_q.put(sentence)
        tail = self.splitter.flush()
        if tail:
            self.sentence_q.put(tail)
        return True

    def _stream_llm(self):
        def on_content(text):
            if self.stale:
                return
            self.reply_text += text
            if not self._staged:
                self._staged = True
                self.timers.mark("first_token")
            for sentence in self.splitter.feed(text):
                self.sentence_q.put(sentence)

        messages = self._messages()
        self.stats = llm.stream_reply(messages, on_content, abort=self.abort)
        if self.stale:
            return
        tail = self.splitter.flush()
        if tail:
            self.sentence_q.put(tail)

    def _messages(self):
        if config.MEMORY_ENABLED and self.bridge.memory_store is not None:
            writer = self.bridge.memory_writer
            recent_cache = (writer.recent_cache.get(self.session_id)
                            if writer is not None else None)
            try:
                messages, info = memory_context.build_for_turn(
                    self.bridge.memory_store, config.DEFAULT_PERSONA,
                    self.session_id, self.user_text,
                    budget_tokens=config.MEMORY_INPUT_BUDGET_TOKENS,
                    recent_cache=recent_cache,
                    token_cache=self.bridge.token_cache,
                    current_input_max_chars=config.MEMORY_CURRENT_INPUT_MAX_CHARS,
                )
                if config.MEMORY_DEBUG:
                    print(memory_context.format_debug(info))
                if not info.get("fallback"):
                    self.selected_ids = list(info.get("selected_ids") or ())
                    return messages
            except Exception:
                pass
        return [{"role": "system", "content": config.DEFAULT_PERSONA},
                {"role": "user", "content": self.user_text}]

    def _tts_stage(self):
        first = True
        try:
            while True:
                sentence = self.sentence_q.get()
                if sentence is STOP or self.stale:
                    break
                if sentence is None:
                    continue
                if first:
                    self.timers.mark("first_sentence")
                pcm, meta = self.bridge.tts.synthesize(sentence)
                if first:
                    self.timers.mark("first_tts_done")
                    self.stats["first_tts_ms"] = meta["tts_ms"]
                    self.stats["engine"] = meta["engine"]
                    first = False
                if self.stale:
                    break
                for i in range(0, len(pcm), CHUNK_BYTES):
                    self.audio_q.put(pcm[i:i + CHUNK_BYTES])
        except Exception as exc:
            self.error = "tts %s: %s" % (type(exc).__name__, exc)
        finally:
            self.audio_q.put(STOP)

    def pcm_chunks(self):
        """Yield PCM16/16k/mono blocks until the turn ends."""
        delivered = False
        while True:
            block = self.audio_q.get()
            if block is STOP:
                if not self.stale and self.error is None:
                    self.completed = True
                break
            if self.stale:
                break
            if not delivered:
                delivered = True
                self.timers.mark("first_byte")
            yield block


class Bridge:
    def __init__(self, tts, memory_store=None, token_cache=None,
                 memory_writer=None):
        self.tts = tts
        self.memory_store = memory_store
        self.token_cache = token_cache
        self.memory_writer = memory_writer
        self.gen = 0
        self._lock = threading.Lock()
        self._active = None

    def superseded(self, gen):
        return gen != self.gen

    def begin(self, user_text, timers=None, session_id="anon"):
        with self._lock:
            self.stop_locked()
            self.gen += 1
            turn = Turn(self, self.gen, user_text, timers or Timers(),
                        session_id=session_id)
            self._active = turn
            turn.start()
            return turn

    def stop_locked(self):
        if self._active is not None:
            self._active.abort.set()
            self._active = None

    def interrupt(self):
        with self._lock:
            self.gen += 1
            self.stop_locked()
        return self.gen
