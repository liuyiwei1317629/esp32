import argparse
import hmac
import io
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import console_hub
import device_seen
import emotion
import file_access
import llm
import memory_maintenance
import memory_score
import memory_store
import memory_writer
import pipeline
import playback
import screen_config
import screen_hub
import screen_http
import songs
import usage
import usage_report
import winperf
from pipeline import Bridge, Timers
from tts import Tts

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
RESTART_CMD = os.path.join(os.path.dirname(BRIDGE_DIR), "start-bridge.cmd")
RELAUNCH_PY = os.path.join(BRIDGE_DIR, "relaunch.py")


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


def build_report(timers, turn, text=None):
    """把一轮遥测拼成日志行；`report()` 与调试台共用同一份来源。"""
    m = timers.marks
    lines = ["=== turn gen=%s ===" % turn.gen]
    if text:
        lines.append("  user: %s" % text)
    if getattr(turn, "local_command", ""):
        lines.append("  local_command=%s" % turn.local_command)
    if getattr(turn, "local_plugin", False):
        mode = ("fixed-fallback" if getattr(turn, "local_plugin_failed", False)
                else "model")
        lines.append("  local_plugin=%s" % mode)
    if turn.error:
        lines.append("  ERROR %s" % turn.error)
    for key, label in (("asr", "ASR完成"), ("first_token", "LLM首字"),
                       ("local_fallback", "本地接管"),
                       ("first_sentence", "收满首句"), ("first_tts_done", "首句合成完"),
                       ("first_byte", "首字节送达")):
        lines.append("  %-12s %s" % (label, fmt_ms(m.get(key))))
    lines.append("  FIRST_AUDIO_MS = %s" % m.get("first_byte"))
    lines.append("  engine=%s  tts_ms=%s  ttft=%.3fs  chunks=%s  usage=%s" % (
        turn.stats.get("engine"), turn.stats.get("first_tts_ms"),
        turn.stats.get("first_token_seconds") or 0.0, turn.stats.get("chunks"),
        json.dumps(turn.stats.get("usage"), ensure_ascii=False)))
    lines.append("  total=%.1fs  reply_chars=%s  finish=%s" % (
        turn.stats.get("total_seconds") or 0.0, turn.stats.get("content_chars"),
        turn.stats.get("finish_reason")))
    lines.append("  brain=%s  fallback=%s  attempts=%s" % (
        turn.stats.get("profile"), turn.stats.get("fallback_used"),
        turn.stats.get("attempts")))
    return lines


def report(timers, turn, text=None, source="device", pcm_bytes=0):
    """打印本轮遥测，同时喂给调试台（调试台故障不影响打印）。"""
    lines = build_report(timers, turn, text)
    for line in lines:
        print(line)
    try:
        hub = console_hub.hub()
        hub.record(console_hub.snapshot(timers, turn, text,
                                        source=source, pcm_bytes=pcm_bytes))
        hub.log(lines)
    except Exception as exc:
        print("  WARN console: %s: %s" % (type(exc).__name__, exc))


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


def device_tracker():
    """设备最近露面（IP + 时间）；只用于顶栏展示，出错就当没有。"""
    try:
        return device_seen.DeviceSeen(config.DEVICE_SEEN_FILE)
    except Exception:
        return None


def build_bridge(tts):
    # 授权目录只读检索：功能关掉时连对象都不建；开启也只在本机读文件。
    access = file_access.FileAccess() if config.FILE_ACCESS_ENABLED else None
    if access is not None:
        try:
            print("file access: %d folder(s) enabled"
                  % len(access.store.enabled_folders()))
        except Exception:
            pass
    if not config.MEMORY_ENABLED:
        print("memory: disabled")
        bridge = Bridge(tts, file_access=access)
        bridge.device_seen = device_tracker()
        return bridge
    store = memory_store.MemoryStore(config.MEMORY_DIR)
    warn_if_memory_read_only(store)
    writer = memory_writer.MemoryWriter(
        store,
        dry_run=config.MEMORY_DRY_RUN or store.read_only,
        debug=config.MEMORY_DEBUG,
        maintenance=memory_maintenance.MemoryMaintenance(store),
    )
    print("memory: enabled dir=%s dry_run=%s" % (config.MEMORY_DIR, writer.dry_run))
    bridge = Bridge(
        tts,
        memory_store=store,
        token_cache=memory_score.TokenCache(),
        memory_writer=writer,
        file_access=access,
    )
    bridge.device_seen = device_tracker()
    return bridge


def enqueue_memory(bridge, turn):
    """终块发送前入队一份不可变快照；任何异常都不影响回复。"""
    writer = getattr(bridge, "memory_writer", None)
    if writer is None or not getattr(turn, "completed", False):
        return False
    # 报时/停止等本地命令不是对话内容，不写入长期记忆。
    if getattr(turn, "local_command", ""):
        return False
    # 本地应急模式没有云端人格与长期记忆，不能污染记忆档案。
    if getattr(turn, "local_plugin", False):
        return False
    try:
        return writer.enqueue(memory_writer.WriteJob.from_turn(turn))
    except Exception as exc:
        print("  WARN memory writer: %s: %s" % (type(exc).__name__, exc))
        return False


def stop_memory(bridge):
    """退出前补刷写队列；dry-run 下同样安全。"""
    writer = getattr(bridge, "memory_writer", None)
    if writer is None:
        return False
    try:
        writer.stop()
        return True
    except Exception as exc:
        print("  WARN memory writer stop: %s: %s" % (type(exc).__name__, exc))
        return False


def build_screen():
    """屏幕推流：默认关闭，改了 SCREEN_ENABLED 才建对象。"""
    if not screen_config.SCREEN_ENABLED:
        print("screen: disabled (SCREEN_ENABLED=0)")
        return None, None
    lib = screen_hub.AnimLibrary(screen_config.FRAMES_DIR)
    hub = screen_hub.ScreenHub(lib)
    streamer = screen_http.Streamer(hub)
    streamer.start()
    print("screen: enabled anims=%d fps=%d" % (len(lib.index), screen_config.FPS))
    return hub, streamer


def _shutdown_soon(httpd, delay=0.2):
    """等 200 先发出去，再停 serve_forever；main() 随后负责关掉各个 worker。"""
    time.sleep(delay)
    try:
        httpd.shutdown()
    except Exception as exc:
        print("  WARN shutdown: %s: %s" % (type(exc).__name__, exc))


def _spawn_relauncher():
    """分离进程：等本进程退出、端口空出来，再重开 start-bridge.cmd。"""
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    cmd = [sys.executable, RELAUNCH_PY, "--pid", str(os.getpid()),
           "--port", str(config.BRIDGE_PORT), "--cmd", RESTART_CMD]
    quiet = dict(close_fds=True, cwd=BRIDGE_DIR, stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    try:
        subprocess.Popen(cmd, creationflags=flags | breakaway, **quiet)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **quiet)


def _memory_item(record):
    """记忆条目的展示投影；原文只在显式请求时返回，且截断。"""
    if not isinstance(record, dict):
        return {"text": str(record)[:200]}
    return {
        "id": record.get("id"),
        "kind": record.get("kind"),
        "slot": record.get("slot"),
        "text": (record.get("text") or "")[:300],
        "pinned": bool(record.get("pinned")),
        "hits": record.get("hits"),
        "created_at": record.get("created_at"),
        "last_used_at": record.get("last_used_at"),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bridge = None
    asr = None
    screen_hub = None
    screen_streamer = None

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
        if route not in ("/turn", "/stop", "/brain", "/api/text",
                         "/api/memory/fact", "/api/memory/pin",
                         "/api/memory/delete", "/shutdown", "/restart",
                         "/api/volume", "/screen/key",
                         "/api/songs", "/api/songs/delete", "/api/persona",
                         "/api/permissions/add", "/api/permissions/toggle",
                         "/api/permissions/remove") or not self._authorized():
            return self._reply(401, b'{"error":"bad auth"}', close=True)
        length = int(self.headers.get("Content-Length") or 0)
        if route == "/api/songs" and length > songs.MAX_UPLOAD_BYTES + 1024:
            return self._reply(413, b'{"error":"file too large"}', close=True)
        body = self.rfile.read(length) if length else b""

        # 只有局域网里的设备（不是本机页面）才算「设备露面」
        if route in ("/turn", "/stop") and not self._is_loopback():
            self._note_device()

        if route == "/shutdown":
            return self._handle_shutdown()

        if route == "/restart":
            return self._handle_restart()

        if route.startswith("/api/permissions"):
            return self._handle_permissions(route, body)

        if route == "/api/volume":
            return self._handle_volume(body)

        if route == "/api/persona":
            return self._handle_persona(body)

        if route == "/brain":
            return self._handle_brain(body)

        if route == "/screen/key":
            if self.screen_hub is None:
                return self._reply(503, b'{"error":"screen disabled"}')
            return screen_http.handle_key(self, self.screen_hub)

        if route.startswith("/api/memory/"):
            return self._handle_memory_write(route, body)

        if route == "/stop":
            gen = self.bridge.interrupt()
            return self._reply(200, json.dumps({"stopped_gen": gen}).encode())

        if route == "/api/text":
            return self._handle_text(body)

        if route.startswith("/api/songs"):
            return self._handle_songs(route, body)

        timers = Timers()
        try:
            text, asr_seconds = self.asr.transcribe_wav(body)
        except Exception as exc:
            return self._reply(400, json.dumps({"error": "asr %s" % exc}).encode())
        timers.mark("asr")
        if not text:
            return self._reply(204, b'{"error":"empty transcript"}')
        song = songs.pick(text)
        if song is not None:
            return self._stream_song(song)
        return self._stream_turn(timers, text, source="device")
    def _handle_songs(self, route, body):
        """加歌 / 删歌只允许本机：会往磁盘写音频，局域网设备无权。"""
        if not self._is_loopback():
            return self._reply(403, b'{"error":"songs edit is loopback-only"}')
        if route == "/api/songs/delete":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                name = str(payload.get("file") or "")
            except (UnicodeDecodeError, ValueError, AttributeError):
                return self._reply(400, b'{"error":"bad json"}')
            try:
                return self._reply_json(200, songs.remove_song(name))
            except KeyError:
                return self._reply(404, b'{"error":"song not found"}')
        _, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        title = (params.get("title") or [""])[0]
        name = (params.get("name") or [""])[0]
        try:
            entry = songs.add_song(title, name, body)
        except ValueError as exc:
            return self._reply_json(400, {"error": str(exc)})
        entry.pop("path", None)
        print("  song add: %s (%s)" % (entry["file"], entry["title"]))
        sys.stdout.flush()
        return self._reply_json(200, entry)

    def _handle_persona(self, body):
        """人设词：只能本机改；空文本等于恢复默认。"""
        if not self._is_loopback():
            return self._reply(403, b'{"error":"persona is loopback-only"}')
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            text = payload.get("text", "")
        except (UnicodeDecodeError, ValueError, AttributeError):
            return self._reply(400, b'{"error":"bad json"}')
        if not isinstance(text, str):
            return self._reply(400, b'{"error":"text must be a string"}')
        text = text.strip()
        if len(text) > config.PERSONA_MAX_CHARS:
            return self._reply_json(400, {
                "error": "最多 %d 字，现在是 %d 字"
                         % (config.PERSONA_MAX_CHARS, len(text))})
        config.save_persona(text)
        return self._reply_json(200, self._persona_payload())

    def _persona_payload(self):
        return {
            "text": config.persona_text(),
            "default": config.DEFAULT_PERSONA,
            "max_chars": config.PERSONA_MAX_CHARS,
            "custom": os.path.isfile(config.PERSONA_FILE),
            "file": config.PERSONA_FILE,
        }

    def _handle_shutdown(self):
        """一键关闭：只允许本机。先回响应，再在后台收摊，避免客户端拿不到 200。"""
        try:
            loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except (ValueError, IndexError):
            loopback = False
        if not loopback:
            return self._reply(403, b'{"error":"shutdown is loopback-only"}')
        try:
            self.bridge.interrupt()
        except Exception:
            pass
        self._reply(200, b'{"ok":true,"stopping":true}')
        threading.Thread(target=_shutdown_soon, args=(self.server,),
                         daemon=True).start()

    def _handle_restart(self):
        """一键重启：只允许本机。先拉起等候进程，再回响应，最后收摊。"""
        if not self._is_loopback():
            return self._reply(403, b'{"error":"restart is loopback-only"}')
        if not os.path.isfile(RESTART_CMD):
            return self._reply(409, b'{"error":"start-bridge.cmd not found"}')
        try:
            self.bridge.interrupt()
        except Exception:
            pass
        try:
            _spawn_relauncher()
        except Exception as exc:
            return self._reply(500, json.dumps(
                {"error": "relaunch %s: %s" % (type(exc).__name__, exc)}).encode())
        self._reply(200, b'{"ok":true,"restarting":true}')
        threading.Thread(target=_shutdown_soon, args=(self.server, 0.5),
                         daemon=True).start()

    def _handle_volume(self, body):
        """放音音量档位：只允许本机页面改；只衰减，不改固件。"""
        if not self._is_loopback():
            return self._reply(403, b'{"error":"volume is loopback-only"}')
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            level = playback.set_level(payload.get("level"))
        except (UnicodeDecodeError, ValueError, AttributeError) as exc:
            return self._reply(400, json.dumps({"error": str(exc)}).encode())
        return self._reply(200, json.dumps({"ok": True, "level": level}).encode())

    # ---- 授权目录（AI 只读权限） ------------------------------------------

    def _is_loopback(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except (ValueError, IndexError):
            return False

    def _note_device(self):
        track = getattr(self.bridge, "device_seen", None)
        if track is None:
            return
        try:
            track.record(self.client_address[0])
        except Exception:
            pass

    def _perms_store(self):
        """优先复用 bridge 里的 store；功能关掉时也能看清单。"""
        access = getattr(self.bridge, "file_access", None)
        return getattr(access, "store", None) or file_access.PermissionStore()

    def _permissions_payload(self):
        store = self._perms_store()
        items = store.items()
        return {
            "enabled": config.FILE_ACCESS_ENABLED,
            "perms_file": store.path,
            "folders": items,
            "active": sum(1 for it in items if it["enabled"] and it["exists"]),
        }

    def _files_summary(self):
        """顶栏只报数量；完整路径清单走 /api/permissions。"""
        if not config.FILE_ACCESS_ENABLED:
            return {"enabled": False, "active": 0}
        try:
            return {"enabled": True,
                    "active": len(self._perms_store().enabled_folders())}
        except Exception as exc:
            return {"enabled": True, "active": 0,
                    "error": "%s: %s" % (type(exc).__name__, exc)}

    def _device_summary(self):
        """最近一次来自设备（局域网）的请求时间与 IP；从没来过就是 null。"""
        track = getattr(self.bridge, "device_seen", None)
        if track is None:
            return None
        try:
            return track.status()
        except Exception:
            return None

    def _dirs_payload(self):
        """目录浏览只列子目录名，不读文件内容；用于面板里手动选文件夹。"""
        query = urllib.parse.parse_qs(self.path.partition("?")[2] or "")
        raw = (query.get("path") or [""])[0].strip().strip('"')
        path = os.path.abspath(os.path.expanduser(raw)) if raw else os.path.expanduser("~")
        if not os.path.isdir(path):
            return {"error": "not a folder: %s" % path}
        dirs = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if not entry.is_dir():
                            continue
                    except OSError:
                        continue
                    dirs.append({"name": entry.name, "path": entry.path})
        except OSError as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}
        dirs.sort(key=lambda d: d["name"].lower())
        parent = os.path.dirname(path)
        return {"path": path, "parent": parent if parent != path else "",
                "dirs": dirs[:500], "truncated": len(dirs) > 500}

    def _handle_permissions(self, route, body):
        """权限改动只允许本机：它决定 AI 能读到哪些文件。"""
        if not self._is_loopback():
            return self._reply(403, b'{"error":"permissions are loopback-only"}')
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return self._reply(400, b'{"error":"bad json"}')
        if not isinstance(payload, dict):
            return self._reply(400, b'{"error":"bad json"}')
        store = self._perms_store()
        try:
            if route == "/api/permissions/add":
                result = store.add(payload.get("path") or "",
                                   label=payload.get("label") or "",
                                   confirm=payload.get("confirm") is True)
            elif route == "/api/permissions/toggle":
                result = store.set_enabled(str(payload.get("id") or ""),
                                           bool(payload.get("enabled")))
            else:
                result = store.remove(str(payload.get("id") or ""))
        except ValueError as exc:
            return self._reply(400, json.dumps({"error": str(exc)}).encode("utf-8"))
        except OSError as exc:
            return self._reply(500, json.dumps({"error": str(exc)}).encode("utf-8"))
        if result is None or result is False:
            return self._reply(404, b'{"error":"folder not found"}')
        if result is True:
            result = {"ok": True}
        result["permissions"] = self._permissions_payload()
        return self._reply_json(200, result)

    def _handle_memory_write(self, route, body):
        """记忆写操作只允许本机：这些是个人数据，局域网设备无权改。"""
        try:
            loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except (ValueError, IndexError):
            loopback = False
        if not loopback:
            return self._reply(403, b'{"error":"memory edit is loopback-only"}')
        store = getattr(self.bridge, "memory_store", None)
        if store is None:
            return self._reply(409, b'{"error":"memory is disabled"}')
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return self._reply(400, b'{"error":"bad json"}')
        if not isinstance(payload, dict):
            return self._reply(400, b'{"error":"bad json"}')
        try:
            if route == "/api/memory/fact":
                result = self._memory_save_fact(store, payload)
            elif route == "/api/memory/pin":
                result = self._memory_set_pin(store, payload)
            else:
                result = self._memory_delete(store, payload)
        except ValueError as exc:
            return self._reply(400, json.dumps({"error": str(exc)}).encode("utf-8"))
        except memory_store.MemoryReadOnly:
            return self._reply(409, b'{"error":"memory is read-only"}')
        except memory_store.MemoryBusy:
            return self._reply(503, b'{"error":"memory lock is busy"}')
        if result is None:
            return self._reply(404, b'{"error":"record not found"}')
        return self._reply(200, json.dumps(result, ensure_ascii=False).encode("utf-8"))

    def _memory_save_fact(self, store, payload):
        kind = str(payload.get("kind") or "fact").strip()
        text = str(payload.get("text") or "").strip()
        slot = str(payload.get("slot") or "").strip() or None
        pinned = bool(payload.get("pinned"))
        replace_id = str(payload.get("replace_id") or "").strip()
        old = None
        if replace_id:
            old = store.find_fact(replace_id)
            if old is None:
                return None
            # 正文改了就是另一条 id；旧的归档而不是直接抹掉，历史留在 tombstone 里。
            if old.get("status") != "active":
                return None
        record = store.upsert_fact(kind, text, slot=slot, pinned=pinned,
                                   source="manual", explicit=True)
        if not pinned and record.get("pinned"):
            store.set_pinned(record["id"], False)
            record["pinned"] = False
        if old is not None and old.get("id") != record.get("id"):
            store.delete_fact(old["id"], reason="superseded")
        return {"ok": True, "item": _memory_item(record)}

    def _memory_set_pin(self, store, payload):
        record_id = str(payload.get("id") or "").strip()
        if not record_id:
            raise ValueError("id is required")
        pinned = bool(payload.get("pinned"))
        if not store.set_pinned(record_id, pinned):
            return None
        return {"ok": True, "id": record_id, "pinned": pinned}

    def _memory_delete(self, store, payload):
        record_id = str(payload.get("id") or "").strip()
        if not record_id:
            raise ValueError("id is required")
        if store.find_fact(record_id) is not None:
            return {"ok": store.delete_fact(record_id, reason="deleted"),
                    "id": record_id}
        if store.find_episode(record_id) is not None:
            return {"ok": store.delete_episode(record_id, reason="deleted"),
                    "id": record_id}
        return None

    def _handle_text(self, body):
        """调试台文字直通：跳过 ASR，其它与 /turn 完全同一条流水线。"""
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            text = str(payload.get("text") or "").strip()
        except (UnicodeDecodeError, ValueError, AttributeError):
            return self._reply(400, b'{"error":"bad json"}')
        if not text:
            return self._reply(400, b'{"error":"text is required"}')
        song = songs.pick(text)
        if song is not None:
            return self._stream_song(song, source="text")
        return self._stream_turn(Timers(), text, source="text")

    def _stream_turn(self, timers, text, source):
        if config.MEMORY_ENABLED:
            turn = self.bridge.begin(
                text, timers,
                session_id=memory_store.parse_session_id(self.path))
        else:
            turn = self.bridge.begin(text, timers)
        # 只有设备发起的回合才动屏：调试台打字是电脑在说话，屏上不该跟着变。
        hub = self.screen_hub if source == "device" else None
        if hub is not None:
            hub.turn_begin()
        session = self._session()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Voice", "s16le/16000/mono")
        self.send_header("X-Session-Id", session)
        self.send_header("X-Gen-Id", str(turn.gen))
        self.end_headers()
        pcm_bytes = 0
        playing = False
        try:
            for block in turn.pcm_chunks():
                if hub is not None and not playing:
                    hub.first_audio(emotion=emotion.tag(turn.reply_text))
                    playing = True
                pcm_bytes += len(block)
                self._chunk(playback.apply(block))
            enqueue_memory(self.bridge, turn)
        except (BrokenPipeError, ConnectionResetError):
            turn.abort.set()
        finally:
            if hub is not None:
                hub.turn_end(ok=not turn.error)
            try:
                entry = usage.record_turn(turn.stats, price_lookup=llm.profile_prices)
                if entry:
                    print("  ledger=%s" % json.dumps(entry, ensure_ascii=False))
            except Exception as exc:
                print("  WARN usage ledger: %s: %s" % (type(exc).__name__, exc))
            self._chunk(b"")
        report(timers, turn, text, source=source, pcm_bytes=pcm_bytes)
        sys.stdout.flush()

    def _stream_song(self, song, source="device"):
        """点播原唱：把 PCM 文件按 /turn 的同一套契约流回去，不走 TTS。"""
        hub = self.screen_hub if source == "device" else None
        if hub is not None:
            hub.first_audio("正在播放 · %s" % song["title"])
        session = self._session()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Voice", "s16le/16000/mono")
        self.send_header("X-Session-Id", session)
        self.end_headers()
        total = 0
        try:
            with open(song["path"], "rb") as fh:
                while True:
                    block = fh.read(16384)
                    if not block:
                        break
                    total += len(block)
                    self._chunk(playback.apply(block))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if hub is not None:
                hub.turn_end()
            try:
                self._chunk(b"")
            except (BrokenPipeError, ConnectionResetError):
                pass
        print("song: %s (%.1f s, %d B)" % (song["title"], total / 32000.0, total))
        sys.stdout.flush()

    # ---- 调试台只读接口 ----------------------------------------------------
    def do_GET(self):
        route = self._route()
        if route in ("/", "/console", "/console/"):
            return self._serve_console()
        if route == "/screen":
            if not self._authorized():
                return self._reply(401, b'{"error":"bad auth"}', close=True)
            if self.screen_streamer is None:
                return self._reply(503, b'{"error":"screen disabled"}', close=True)
            return screen_http.write_stream(self, self.screen_streamer)
        if route not in ("/api/state", "/api/turns", "/api/memory",
                         "/api/usage", "/api/songs", "/api/persona",
                         "/api/permissions", "/api/permissions/dirs"):
            return self._reply(404, b'{"error":"not found"}')
        if not self._authorized():
            return self._reply(401, b'{"error":"bad auth"}', close=True)
        if route == "/api/state":
            return self._reply_json(200, self._state())
        if route == "/api/turns":
            return self._reply_json(200, self._turns_payload())
        if route == "/api/usage":
            return self._reply_json(200, usage_report.report())
        if route == "/api/songs":
            if not self._is_loopback():
                return self._reply(403, b'{"error":"songs are loopback-only"}')
            return self._reply_json(200, {"songs": songs.catalog()})
        if route == "/api/persona":
            if not self._is_loopback():
                return self._reply(403, b'{"error":"persona is loopback-only"}')
            return self._reply_json(200, self._persona_payload())
        if route.startswith("/api/permissions"):
            # 目录清单暴露本机文件结构，只给本机页面看。
            if not self._is_loopback():
                return self._reply(403, b'{"error":"permissions are loopback-only"}')
            if route.endswith("/dirs"):
                return self._reply_json(200, self._dirs_payload())
            return self._reply_json(200, self._permissions_payload())
        return self._reply_json(200, self._memory_payload())

    def _serve_console(self):
        path = os.path.join(WEBUI_DIR, "console.html")
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError:
            return self._reply(500, b"webui/console.html not found",
                               "text/plain; charset=utf-8")
        # 本机打开时把 token 直接注入页面，省掉复制粘贴；
        # 本机用户本来就能读 token 文件，不构成额外暴露。
        token = ""
        try:
            if ipaddress.ip_address(self.client_address[0]).is_loopback:
                token = config.auth_token() or ""
        except (ValueError, IndexError):
            token = ""
        blob = blob.replace(b"__BRIDGE_TOKEN__", token.encode("utf-8"))
        return self._reply(200, blob, "text/html; charset=utf-8")

    def _reply_json(self, code, payload):
        blob = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        return self._reply(code, blob)

    def _state(self):
        hub = console_hub.hub()
        tts = getattr(self.bridge, "tts", None)
        edge = getattr(tts, "edge", None)
        cosy = getattr(tts, "cosy", None)
        feiyu = getattr(tts, "feiyu", None)
        store = getattr(self.bridge, "memory_store", None)
        try:
            brain = llm.brain_status()
            profiles = llm.profile_names()
            # 只给展示用的模型名与价格，绝不下发密钥（profile_meta 内部已过滤）
            profile_meta = llm.profile_meta()
        except Exception as exc:
            brain = {"error": "%s: %s" % (type(exc).__name__, exc)}
            profiles = []
            profile_meta = {}
        return {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "uptime_s": hub.uptime_s,
            "brain": brain,
            "profiles": profiles,
            "profile_meta": profile_meta,
            "volume": {"level": playback.level(), "levels": list(playback.LEVELS)},
            "engine": {
                "edge": {"ready": bool(getattr(edge, "ready", False)),
                         "reason": getattr(edge, "dead_reason", None)},
                "feiyu": {"ready": bool(getattr(feiyu, "ready", False)),
                          "reason": getattr(feiyu, "dead_reason", None),
                          "style": config.FEIYU_STYLE,
                          "loading": bool(tts is not None and config.FEIYU_ENABLED
                                          and feiyu is None)},
                "cosyvoice": {"ready": bool(getattr(cosy, "ready", False)),
                              "reason": getattr(cosy, "dead_reason", None),
                              "loading": bool(tts is not None and cosy is None
                                              and not config.FEIYU_ENABLED)},
            },
            "memory": {
                "enabled": config.MEMORY_ENABLED,
                "dry_run": config.MEMORY_DRY_RUN,
                "debug": config.MEMORY_DEBUG,
                "dir": config.MEMORY_DIR if config.MEMORY_ENABLED else None,
                "read_only": bool(getattr(store, "read_only", False)),
            },
            "files": self._files_summary(),
            "device": self._device_summary(),
            "config": {
                "sentence_first_min": config.SENTENCE_FIRST_MIN_CHARS,
                "sentence_second_min": config.SENTENCE_SECOND_MIN_CHARS,
                "sentence_second_max": config.SENTENCE_SECOND_MAX_CHARS,
                "sentence_min": config.SENTENCE_MIN_CHARS,
                "cosyvoice_timeout": config.COSYVOICE_TIMEOUT,
                "cosyvoice_speaker": config.COSYVOICE_SPEAKER,
                "feiyu_style": config.FEIYU_STYLE,
                "feiyu_timeout": config.FEIYU_TIMEOUT,
                "edge_voice": config.EDGE_VOICE,
                "local_plugin": config.LOCAL_PLUGIN_ENABLED,
                "sample_rate": config.SAMPLE_RATE,
                "port": config.BRIDGE_PORT,
            },
            "counts": hub.counts(),
            "record_path": hub.path,
        }

    def _query_int(self, name, default=0):
        query = urllib.parse.parse_qs(self.path.partition("?")[2])
        try:
            return int(query.get(name, [default])[0])
        except (TypeError, ValueError):
            return default

    def _turns_payload(self):
        return console_hub.hub().since(self._query_int("since"),
                                       self._query_int("log_since"),
                                       limit=200)

    def _memory_payload(self):
        """默认只给计数；`?text=1` 才回原文（个人数据，需主动打开）。"""
        store = getattr(self.bridge, "memory_store", None)
        if store is None:
            return {"enabled": False}
        query = urllib.parse.parse_qs(self.path.partition("?")[2] or "")
        with_text = (query.get("text") or ["0"])[0].lower() in ("1", "true", "yes")
        want_kind = (query.get("kind") or [""])[0].strip()
        needle = (query.get("q") or [""])[0].strip().lower()
        try:
            facts = store.active_facts()
            episodes = store.active_episodes()
        except Exception as exc:
            return {"enabled": True,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        payload = {
            "enabled": True,
            "dry_run": config.MEMORY_DRY_RUN,
            "read_only": bool(getattr(store, "read_only", False)),
            "facts": len(facts),
            "episodes": len(episodes),
            "with_text": with_text,
        }

        def keep(item):
            if want_kind and item.get("kind") != want_kind:
                return False
            return not needle or needle in str(item.get("text") or "").lower()

        if with_text:
            payload["fact_items"] = [_memory_item(f) for f in facts if keep(f)][:200]
            payload["episode_items"] = [_memory_item(e) for e in episodes if keep(e)][:200]
        return payload


def serve(bridge, asr):
    Handler.bridge = bridge
    Handler.asr = asr
    screen, streamer = build_screen()
    Handler.screen_hub = screen
    Handler.screen_streamer = streamer
    if config.auth_token() is None:
        print("WARN: BRIDGE_TOKEN not set; %s:%s accepts unauthenticated audio"
              % (config.BRIDGE_HOST, config.BRIDGE_PORT))
    print("brain: %s" % json.dumps(llm.brain_status(), ensure_ascii=False))
    httpd = ThreadingHTTPServer((config.BRIDGE_HOST, config.BRIDGE_PORT), Handler)
    try:
        print("bridge listening on http://%s:%d  (POST /turn, POST /stop, POST /brain%s)"
              % (config.BRIDGE_HOST, config.BRIDGE_PORT,
                 ", GET /screen" if streamer is not None else ""))
        print("console: http://127.0.0.1:%d/console   (调试台，需 bridge_token)"
              % config.BRIDGE_PORT)
        print("console: 轮次落盘 %s" % (console_hub.hub().path or "已关闭"))
        sys.stdout.flush()
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if streamer is not None:
            streamer.stop()
        stop_memory(bridge)
        httpd.server_close()


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
    stop_memory(bridge)
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
    stop_memory(bridge)
    report(timers, turn, text)
    if args.play and out:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, "mic_%s.wav" % time.strftime("%H%M%S"))
        with open(path, "wb") as fh:
            fh.write(pcm_to_wav(bytes(out)))
        play_wav(path)


def main():
    config.force_utf8_output()
    # SenseVoice runs in this process; keep it off the E-cores.
    winperf.unthrottle()
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="listen for ESP32 requests")
    ap.add_argument("--text", help="skip ASR and speak this reply")
    ap.add_argument("--file", help="16k mono WAV in, full pipeline")
    ap.add_argument("--mic", action="store_true", help="capture from local microphone")
    ap.add_argument("--play", action="store_true", help="play the reply through laptop speakers")
    ap.add_argument("--no-cosyvoice", action="store_true", help="skip the CosyVoice fallback")
    ap.add_argument("--no-feiyu", action="store_true", help="skip the Feiyu (WSL) main voice")
    ap.add_argument("--asr-device", default="cpu")
    ap.add_argument("--brain", help="startup brain profile (overrides BRAIN_PROFILE)")
    args = ap.parse_args()

    if args.brain:
        llm.set_profile(args.brain)

    tts = Tts(start_cosyvoice=not args.no_cosyvoice, start_feiyu=not args.no_feiyu)
    try:
        bridge = build_bridge(tts)
        print("edge worker: %s" % ("ready" if tts.edge.ready else tts.edge.dead_reason))
        if not args.no_feiyu:
            print("feiyu worker: loading in background in WSL (~90 s cold)")
        elif not args.no_cosyvoice:
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
    finally:
        tts.stop()


if __name__ == "__main__":
    main()
