import argparse
import hmac
import io
import ipaddress
import json
import os
import subprocess
import sys
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import llm
import memory_score
import memory_store
import memory_writer
import pipeline
import usage
from pipeline import Bridge, Timers
from tts import Tts

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def is_lan_address(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def pcm_to_wav(pcm, rate=config.SAMPLE_RATE):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def fmt_ms(value):
    return "-" if value is None else "%6d ms" % value


def report(timers, turn, text=None):
    m = timers.marks
    print("=== turn gen=%s ===" % turn.gen)
    if text:
        print("  user: %s" % text)
    if turn.error:
        print("  ERROR %s" % turn.error)
    for key, label in (("asr", "ASR完成"), ("first_token", "LLM首字"),
                       ("first_sentence", "收满首句"), ("first_tts_done", "首句合成完"),
                       ("first_byte", "首字节送达")):
        print("  %-12s %s" % (label, fmt_ms(m.get(key))))
    print("  FIRST_AUDIO_MS = %s" % m.get("first_byte"))
    print("  engine=%s  tts_ms=%s  ttft=%.3fs  chunks=%s  usage=%s" % (
        turn.stats.get("engine"), turn.stats.get("first_tts_ms"),
        turn.stats.get("first_token_seconds") or 0.0, turn.stats.get("chunks"),
        json.dumps(turn.stats.get("usage"), ensure_ascii=False)))
    print("  total=%.1fs  reply_chars=%s  finish=%s" % (
        turn.stats.get("total_seconds") or 0.0, turn.stats.get("content_chars"),
        turn.stats.get("finish_reason")))
    print("  brain=%s  fallback=%s  attempts=%s" % (
        turn.stats.get("profile"), turn.stats.get("fallback_used"),
        turn.stats.get("attempts")))


def play_wav(path):
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "(New-Object Media.SoundPlayer '%s').PlaySync()" % path], check=False)


def warn_if_memory_read_only(store):
    if store is None or not store.read_only:
        return None
    state = store.state
    message = (
        "memory state schema_version=%s is newer than supported %s; "
        "running read-only: %s"
        % (state.get("schema_version"), memory_store.SCHEMA_VERSION,
           store.state_path)
    )
    print("WARN: " + message)
    return message


def build_bridge(tts):
    if not config.MEMORY_ENABLED:
        return Bridge(tts)
    store = memory_store.MemoryStore(config.MEMORY_DIR)
    warn_if_memory_read_only(store)
    writer = memory_writer.MemoryWriter(
        store,
        dry_run=config.MEMORY_DRY_RUN or store.read_only,
        debug=config.MEMORY_DEBUG,
    )
    return Bridge(
        tts,
        memory_store=store,
        token_cache=memory_score.TokenCache(),
        memory_writer=writer,
    )


def enqueue_memory(bridge, turn):
    """终块发送前入队一份不可变快照；任何异常都不影响回复。"""
    writer = getattr(bridge, "memory_writer", None)
    if writer is None or not getattr(turn, "completed", False):
        return False
    try:
        return writer.enqueue(memory_writer.WriteJob.from_turn(turn))
    except Exception as exc:
        print("  WARN memory writer: %s: %s" % (type(exc).__name__, exc))
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bridge = None
    asr = None

    def log_message(self, fmt, *args):
        pass

    def _route(self):
        return self.path.split("?")[0]

    def _session(self):
        _, _, query = self.path.partition("?")
        for pair in query.split("&"):
            if pair.startswith("session_id="):
                return pair[9:64]
        return "anon"

    def _reply(self, code, blob, ctype="application/json", close=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(blob)

    def _authorized(self):
        token = config.auth_token()
        if not token:
            return True
        return hmac.compare_digest(self.headers.get("X-Auth", ""), token)

    def _chunk(self, data):
        self.wfile.write(b"%X\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")

    def _handle_brain(self, body):
        if not is_lan_address(self.client_address[0]):
            return self._reply(403, b'{"error":"brain switch is LAN-only"}')
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            profile = str(payload.get("profile") or "").strip()
        except (UnicodeDecodeError, ValueError, AttributeError):
            return self._reply(400, b'{"error":"bad json"}')
        if not profile:
            return self._reply(400, b'{"error":"profile is required"}')
        try:
            active = llm.set_profile(profile)
        except ValueError as exc:
            return self._reply(400, json.dumps({"error": str(exc)}).encode())
        status = dict(llm.brain_status())
        status["active"] = active
        return self._reply(200, json.dumps(status, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):
        route = self._route()
        if route not in ("/turn", "/stop", "/brain") or not self._authorized():
            return self._reply(401, b'{"error":"bad auth"}', close=True)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if route == "/brain":
            return self._handle_brain(body)

        session = self._session()

        if route == "/stop":
            gen = self.bridge.interrupt()
            return self._reply(200, json.dumps({"stopped_gen": gen}).encode())

        timers = Timers()
        try:
            text, asr_seconds = self.asr.transcribe_wav(body)
        except Exception as exc:
            return self._reply(400, json.dumps({"error": "asr %s" % exc}).encode())
        timers.mark("asr")
        if not text:
            return self._reply(204, b'{"error":"empty transcript"}')

        if config.MEMORY_ENABLED:
            turn = self.bridge.begin(
                text, timers,
                session_id=memory_store.parse_session_id(self.path))
        else:
            turn = self.bridge.begin(text, timers)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Voice", "s16le/16000/mono")
        self.send_header("X-Session-Id", session)
        self.send_header("X-Gen-Id", str(turn.gen))
        self.end_headers()
        try:
            for block in turn.pcm_chunks():
                self._chunk(block)
            enqueue_memory(self.bridge, turn)
        except (BrokenPipeError, ConnectionResetError):
            turn.abort.set()
        finally:
            try:
                entry = usage.record_turn(turn.stats, price_lookup=llm.profile_prices)
                if entry:
                    print("  ledger=%s" % json.dumps(entry, ensure_ascii=False))
            except Exception as exc:
                print("  WARN usage ledger: %s: %s" % (type(exc).__name__, exc))
            self._chunk(b"")
        report(timers, turn, text)
        sys.stdout.flush()


def serve(bridge, asr):
    Handler.bridge = bridge
    Handler.asr = asr
    if config.auth_token() is None:
        print("WARN: BRIDGE_TOKEN not set; %s:%s accepts unauthenticated audio"
              % (config.BRIDGE_HOST, config.BRIDGE_PORT))
    print("brain: %s" % json.dumps(llm.brain_status(), ensure_ascii=False))
    httpd = ThreadingHTTPServer((config.BRIDGE_HOST, config.BRIDGE_PORT), Handler)
    print("bridge listening on http://%s:%d  (POST /turn, POST /stop, POST /brain)"
          % (config.BRIDGE_HOST, config.BRIDGE_PORT))
    sys.stdout.flush()
    httpd.serve_forever()


def run_local(args, bridge, asr):
    timers = Timers()
    if args.text:
        text = args.text
    else:
        with open(args.file, "rb") as fh:
            blob = fh.read()
        text, _ = asr.transcribe_wav(blob)
        timers.mark("asr")
    print("user: %s" % text)
    if config.MEMORY_ENABLED:
        turn = bridge.begin(text, timers, session_id="local")
    else:
        turn = bridge.begin(text, timers)
    pcm = bytearray()
    for block in turn.pcm_chunks():
        pcm += block
    enqueue_memory(bridge, turn)
    report(timers, turn)
    if not pcm:
        print("no audio produced")
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "turn%d_%s.wav" % (turn.gen, time.strftime("%H%M%S")))
    with open(path, "wb") as fh:
        fh.write(pcm_to_wav(bytes(pcm)))
    print("audio: %s (%.2f s)" % (path, len(pcm) / 2 / config.SAMPLE_RATE))
    if args.play:
        play_wav(path)


def record_mic(args, bridge, asr):
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        print("no capture library installed. To test the real mic path:")
        print('  "C:\\Users\\DELL\\voice-ai\\.venv\\Scripts\\python.exe" -m pip install sounddevice')
        return
    rate = int(sd.query_devices(kind="input")["default_samplerate"])
    block = max(160, int(rate * 0.01))
    print("speaking... (silence ends the recording, %d Hz input)" % rate)
    frames, silent, voiced = [], 0.0, False
    with sd.InputStream(samplerate=rate, channels=1, dtype="int16", blocksize=block) as stream:
        while True:
            data, _ = stream.read(block)
            mono = data[:, 0]
            level = float(np.abs(mono).mean()) / 32768.0
            if level > 0.01:
                voiced = True
                silent = 0.0
                frames.append(mono)
            elif voiced:
                silent += block / rate
                frames.append(mono)
                if silent > 0.6:
                    break
    if not voiced:
        print("nothing captured")
        return
    audio = np.concatenate(frames).astype("<i2")
    if rate != config.SAMPLE_RATE:
        import librosa

        audio = librosa.resample(audio.astype(np.float32), orig_sr=rate,
                                 target_sr=config.SAMPLE_RATE)
    pcm = np.clip(audio, -32768, 32767).astype("<i2").tobytes()
    timers = Timers()  # t0 must be the VAD endpoint, not the start of speech
    text, _ = asr.transcribe_pcm(pcm)
    timers.mark("asr")
    if config.MEMORY_ENABLED:
        turn = bridge.begin(text, timers, session_id="local")
    else:
        turn = bridge.begin(text, timers)
    out = bytearray()
    for b in turn.pcm_chunks():
        out += b
    enqueue_memory(bridge, turn)
    report(timers, turn, text)
    if args.play and out:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, "mic_%s.wav" % time.strftime("%H%M%S"))
        with open(path, "wb") as fh:
            fh.write(pcm_to_wav(bytes(out)))
        play_wav(path)


def main():
    config.force_utf8_output()
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="listen for ESP32 requests")
    ap.add_argument("--text", help="skip ASR and speak this reply")
    ap.add_argument("--file", help="16k mono WAV in, full pipeline")
    ap.add_argument("--mic", action="store_true", help="capture from local microphone")
    ap.add_argument("--play", action="store_true", help="play the reply through laptop speakers")
    ap.add_argument("--no-cosyvoice", action="store_true", help="edge-tts only")
    ap.add_argument("--asr-device", default="cpu")
    ap.add_argument("--brain", help="startup brain profile (overrides BRAIN_PROFILE)")
    args = ap.parse_args()

    if args.brain:
        llm.set_profile(args.brain)

    tts = Tts(start_cosyvoice=not args.no_cosyvoice)
    bridge = build_bridge(tts)
    print("edge worker: %s" % ("ready" if tts.edge.ready else tts.edge.dead_reason))
    if not args.no_cosyvoice:
        print("cosyvoice worker: loading in background (~54 s cold)")

    asr = None
    if args.file or args.mic or args.serve:
        from asr_engine import Asr

        print("loading SenseVoice on %s..." % args.asr_device)
        asr = Asr(args.asr_device)

    if args.serve:
        serve(bridge, asr)
    elif args.text or args.file:
        run_local(args, bridge, asr)
    elif args.mic:
        record_mic(args, bridge, asr)
    else:
        ap.print_help()
    tts.stop()


if __name__ == "__main__":
    main()
