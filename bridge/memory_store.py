"""五层记忆的本地存储层（纯标准库）。

设计约束：
- facts / episodes 用 JSONL，每行一条完整记录；sessions 用单文件 JSON。
- 写入先写临时文件、fsync，再 os.replace；保留上一版 .bak。
- 进程内 RLock + 跨进程 state.lock（O_CREAT|O_EXCL，默认 2 s 超时）。
- 读路径按 mtime/size 判断是否需要重载，不每轮全量读盘。
- 未知 kind、损坏行跳过并计数；文件损坏时可用 .bak 恢复。
- tombstone 默认不复活；只有显式“记住”才允许显式复活。

本模块不联网、不调用模型、不读写密钥。
"""

import copy
import hashlib
import json
import os
import shutil
import threading
import time
import unicodedata
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_VERSION = 1
ACTIVE_FACTS_MAX = 200
ACTIVE_EPISODES_MAX = 500
TOMBSTONE_KEEP_DAYS = 365
LOCK_STALE_SECONDS = 10.0

FACT_KINDS = frozenset({"fact", "preference", "relation", "residence", "name"})
EPISODE_KINDS = frozenset({
    "fact", "preference", "relation", "residence", "name",
    "event", "near_term", "todo",
})
ALL_KINDS = FACT_KINDS | EPISODE_KINDS
TOMBSTONE_REASONS = frozenset({
    "deleted", "demoted", "promoted", "expired", "capacity",
})

MAX_TEXT_CHARS = 200


class MemoryError(Exception):
    """记忆层的基类异常。"""


class MemoryBusy(MemoryError):
    """跨进程锁在超时时间内没有拿到。"""


class MemoryReadOnly(MemoryError):
    """state.json 的 schema 版本高于当前代码，拒绝写入。"""


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    """把 datetime/ISO 字符串统一成 aware UTC datetime；解析失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def iso_now(now=None):
    return as_utc(now or utc_now()).isoformat(timespec="seconds")


def parse_iso(value):
    return as_utc(value)


def normalize_text(text):
    """NFKC + 去首尾空白 + 折叠空白 + 去标点，用于 ID 与去重。"""
    if text is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text)).strip()
    kept = []
    for char in normalized:
        category = unicodedata.category(char)
        if category.startswith("P"):
            continue
        if char.isspace():
            if kept and kept[-1] != " ":
                kept.append(" ")
            continue
        kept.append(char)
    return "".join(kept).strip()


def record_id(kind, text):
    payload = "%s:%s" % (str(kind or ""), normalize_text(text))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def parse_session_id(query):
    """从原始 query 中解析 session_id，不依赖 server._session()。

    顺序固定：按第一个 '=' 分离 -> URL decode -> 截 55 字符。
    空值/缺失才返回 anon；不按字符白名单合并到 anon。
    """
    if not query:
        return "anon"
    raw_query = str(query)
    if "?" in raw_query:
        raw_query = raw_query.partition("?")[2]
    for pair in raw_query.split("&"):
        if not pair.startswith("session_id="):
            continue
        raw = urllib.parse.unquote(pair.split("=", 1)[1])
        sid = raw[:55]
        return sid or "anon"
    return "anon"


def session_bucket(session_id):
    sid = str(session_id if session_id is not None else "anon")
    if not sid:
        sid = "anon"
    return hashlib.sha1(sid.encode("utf-8")).hexdigest()[:16]


def clip_text(text, limit):
    if text is None or limit <= 0:
        return ""
    return str(text).strip()[:limit].rstrip()


def _clean_text(text):
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return text.strip()


def _parse_completed_turns(value):
    if value is None or value == "":
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _new_history_entry(record, retired_at):
    return {
        "text": record.get("text", ""),
        "revision": int(record.get("revision") or 1),
        "retired_at": retired_at,
    }


def _replace_with_retry(src, dst):
    # Windows 杀毒/索引会短暂占用目标文件，os.replace 偶发 WinError 5。
    delays = (0.0, 0.02, 0.05, 0.1)
    for index, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if index == len(delays) - 1:
                raise


def make_fact(kind, text, slot=None, pinned=False, source="rule", now=None,
              status="active", tombstone_reason=None, revision=1, history=None,
              created_at=None, updated_at=None, last_used_at=None, hits=0):
    ts = iso_now(now)
    kind = kind if kind in FACT_KINDS else "fact"
    return {
        "id": record_id(kind, text),
        "kind": kind,
        "text": _clean_text(text),
        "slot": slot or None,
        "pinned": bool(pinned),
        "status": status,
        "tombstone_reason": tombstone_reason,
        "revision": int(revision or 1),
        "history": list(history or []),
        "created_at": created_at or ts,
        "updated_at": updated_at or ts,
        "last_used_at": last_used_at,
        "hits": int(hits or 0),
        "source": source or "rule",
    }


def make_episode(kind, text, slot=None, due_at=None, event_at=None,
                 source="rule", now=None, status="active",
                 tombstone_reason=None, revision=1, history=None,
                 created_at=None, updated_at=None, last_used_at=None, hits=0,
                 fact_id=None):
    ts = iso_now(now)
    kind = kind if kind in EPISODE_KINDS else "fact"
    return {
        "id": record_id(kind, text),
        "text": _clean_text(text),
        "kind": kind,
        "slot": slot or None,
        "status": status,
        "tombstone_reason": tombstone_reason,
        "revision": int(revision or 1),
        "history": list(history or []),
        "created_at": created_at or ts,
        "updated_at": updated_at or ts,
        "event_at": event_at,
        "due_at": due_at,
        "last_used_at": last_used_at,
        "hits": int(hits or 0),
        "source": source or "rule",
        "fact_id": fact_id,
    }


def _sort_facts(facts):
    def key(item):
        used = parse_iso(item.get("last_used_at"))
        return (
            0 if item.get("pinned") else 1,
            -(used.timestamp() if used else float("-inf")),
            -int(item.get("hits") or 0),
            str(item.get("id") or ""),
        )
    return sorted(facts, key=key)


def _sort_weakest(items):
    def key(item):
        used = parse_iso(item.get("last_used_at"))
        created = parse_iso(item.get("created_at"))
        return (
            int(item.get("hits") or 0),
            used.timestamp() if used else float("-inf"),
            created.timestamp() if created else float("-inf"),
            str(item.get("id") or ""),
        )
    return sorted(items, key=key)


class MemoryStore:
    """一个记忆目录的读写入口。

    单实例线程安全；多个进程通过 state.lock 串行化写操作。
    """

    def __init__(self, root=None, clock=None, lock_timeout=2.0,
                 stale_lock_seconds=LOCK_STALE_SECONDS):
        if root is None:
            root = os.environ.get(
                "MEMORY_DIR",
                os.path.join(os.path.expanduser("~"), ".voice-ai", "memory"),
            )
        self.root = os.path.abspath(os.path.expanduser(str(root)))
        self.sessions_dir = os.path.join(self.root, "sessions")
        self.facts_path = os.path.join(self.root, "facts.jsonl")
        self.episodes_path = os.path.join(self.root, "episodes.jsonl")
        self.state_path = os.path.join(self.root, "state.json")
        self.lock_path = os.path.join(self.root, "state.lock")
        self.clock = clock or utc_now
        self.lock_timeout = float(lock_timeout)
        self.stale_lock_seconds = float(stale_lock_seconds)

        self._guard = threading.RLock()
        self._guard_depth = 0
        self._file_locks = {
            "facts": threading.RLock(),
            "episodes": threading.RLock(),
            "state": threading.RLock(),
        }
        self._session_locks = {}
        self._session_locks_guard = threading.Lock()

        self._cache = {
            "facts": None,
            "episodes": None,
        }
        self._cache_stat = {
            "facts": None,
            "episodes": None,
        }
        self._sessions = {}
        self._session_stat = {}
        self._bad_lines = {"facts": 0, "episodes": 0}
        self._unknown_kinds = 0
        self._state = None
        self._state_stat = None
        self.read_only = False
        self._load_state(initial=True)

    # ------------------------------------------------------------------
    # 路径与状态
    # ------------------------------------------------------------------
    def _now(self, now=None):
        if now is None:
            value = self.clock()
        else:
            value = now
        return as_utc(value) or utc_now()

    def _ensure_root(self):
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.sessions_dir, exist_ok=True)

    def _stat_key(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def default_state(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "last_maintenance": None,
            "counters": {},
            "at_capacity": False,
            "turns_since_maintenance": 0,
        }

    def _load_state(self, initial=False):
        state = self.default_state()
        self.read_only = False
        stat_key = self._stat_key(self.state_path)
        if stat_key is not None:
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict):
                version = raw.get("schema_version", SCHEMA_VERSION)
                try:
                    version = int(version)
                except (TypeError, ValueError):
                    version = SCHEMA_VERSION
                if version > SCHEMA_VERSION:
                    self.read_only = True
                    state["schema_version"] = version
                    state["read_only_reason"] = "schema_version"
                else:
                    state.update({
                        "schema_version": version,
                        "last_maintenance": raw.get("last_maintenance"),
                        "counters": dict(raw.get("counters") or {}),
                        "at_capacity": bool(raw.get("at_capacity")),
                        "turns_since_maintenance": int(
                            raw.get("turns_since_maintenance") or 0),
                    })
        if initial:
            self._state = state
            self._state_stat = stat_key
        else:
            self._state = state
            self._state_stat = stat_key
        return copy.deepcopy(state)

    @property
    def state(self):
        if self._state_stat != self._stat_key(self.state_path):
            self._load_state()
        return copy.deepcopy(self._state)

    def _normalized_state(self, state):
        payload = copy.deepcopy(state)
        payload["schema_version"] = int(payload.get("schema_version") or SCHEMA_VERSION)
        payload.setdefault("counters", {})
        payload.setdefault("last_maintenance", None)
        payload.setdefault("at_capacity", False)
        payload.setdefault("turns_since_maintenance", 0)
        return payload

    def save_state(self, state=None):
        if self.read_only:
            raise MemoryReadOnly("state schema is newer than this code")
        payload = self._normalized_state(
            state if state is not None else (self._state or self.default_state()))
        with self._write_guard():
            self._ensure_root()
            self._atomic_write_json(self.state_path, payload)
            self._state = payload
            self._state_stat = self._stat_key(self.state_path)
        return copy.deepcopy(payload)

    def update_state(self, mutate):
        """锁内重读 state.json 再修改，避免跨实例覆盖彼此的字段。"""
        if self.read_only:
            raise MemoryReadOnly("state schema is newer than this code")
        with self._write_guard():
            current = self._load_state()
            if self.read_only:
                raise MemoryReadOnly("state schema is newer than this code")
            before = copy.deepcopy(current)
            mutate(current)
            if current == before:
                return copy.deepcopy(current)
            payload = self._normalized_state(current)
            self._atomic_write_json(self.state_path, payload)
            self._state = payload
            self._state_stat = self._stat_key(self.state_path)
            return copy.deepcopy(payload)

    def bump_counter(self, name, amount=1, persist=True):
        if not name:
            return

        def apply(state):
            counters = state.setdefault("counters", {})
            counters[str(name)] = int(counters.get(str(name)) or 0) + int(amount)

        if persist and not self.read_only:
            try:
                self.update_state(apply)
            except (MemoryBusy, MemoryReadOnly, OSError):
                self._state_stat = None
        else:
            with self._guard:
                state = self.state
                apply(state)
                self._state = state

    def counters(self):
        return dict(self.state.get("counters") or {})

    def set_at_capacity(self, value):
        if self.read_only:
            raise MemoryReadOnly("state schema is newer than this code")

        def apply(state):
            state["at_capacity"] = bool(value)

        self.update_state(apply)
        return bool(value)

    def can_auto_write(self):
        return (not self.read_only) and (not bool(self.state.get("at_capacity")))

    # ------------------------------------------------------------------
    # 跨进程锁
    # ------------------------------------------------------------------
    @contextmanager
    def _write_guard(self):
        with self._guard:
            if self.read_only:
                raise MemoryReadOnly("state schema is newer than this code")
            first = self._guard_depth == 0
            if first:
                self._acquire_process_lock()
            self._guard_depth += 1
            try:
                yield
            finally:
                self._guard_depth -= 1
                if first:
                    self._release_process_lock()

    def _acquire_process_lock(self):
        self._ensure_root()
        deadline = time.monotonic() + max(0.0, self.lock_timeout)
        while True:
            try:
                fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if self._lock_is_stale():
                    try:
                        os.unlink(self.lock_path)
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise MemoryBusy("memory state.lock is busy")
                time.sleep(0.02)
            else:
                try:
                    payload = "%d %.6f\n" % (os.getpid(), time.time())
                    os.write(fd, payload.encode("ascii", "ignore"))
                finally:
                    os.close(fd)
                return

    def _lock_is_stale(self):
        try:
            stat = os.stat(self.lock_path)
        except OSError:
            return False
        return (time.time() - stat.st_mtime) > self.stale_lock_seconds

    def _release_process_lock(self):
        try:
            os.unlink(self.lock_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 原子写与 JSONL 读取
    # ------------------------------------------------------------------
    def _atomic_write(self, path, text):
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            if os.path.exists(path):
                bak = path + ".bak"
                try:
                    shutil.copyfile(path, bak)
                except OSError:
                    pass
            _replace_with_retry(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass

    def _atomic_write_json(self, path, payload):
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          indent=None) + "\n"
        self._atomic_write(path, text)

    def _read_jsonl(self, path, kind):
        records = []
        bad = 0
        exists = os.path.exists(path)
        size = 0
        if exists:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except ValueError:
                            bad += 1
                            continue
                        if not isinstance(item, dict):
                            bad += 1
                            continue
                        record_kind = item.get("kind")
                        if record_kind not in ALL_KINDS:
                            self._unknown_kinds += 1
                            continue
                        if not item.get("id"):
                            item["id"] = record_id(record_kind, item.get("text", ""))
                        records.append(self._normalize_record(item, kind))
            except OSError:
                bad += 1
        damaged = exists and size > 0 and not records
        missing = not exists
        if (damaged or missing) and os.path.exists(path + ".bak"):
            backup = self._read_jsonl_raw(path + ".bak", kind)
            if backup:
                if exists:
                    corrupt = "%s.corrupt.%d" % (path, int(time.time()))
                    try:
                        os.replace(path, corrupt)
                    except OSError:
                        pass
                try:
                    shutil.copyfile(path + ".bak", path)
                except OSError:
                    pass
                records = backup
                bad = 0
                self.bump_counter("backup_restored", persist=False)
        self._bad_lines[kind] = bad
        return records

    def _read_jsonl_raw(self, path, kind):
        records = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    if item.get("kind") not in ALL_KINDS:
                        continue
                    if not item.get("id"):
                        item["id"] = record_id(item.get("kind"), item.get("text", ""))
                    records.append(self._normalize_record(item, kind))
        except OSError:
            return []
        return records

    def _normalize_record(self, item, kind):
        record = copy.deepcopy(item)
        record["kind"] = record.get("kind") or "fact"
        record["text"] = _clean_text(record.get("text"))
        record["slot"] = record.get("slot") or None
        record["status"] = record.get("status") or "active"
        record["revision"] = int(record.get("revision") or 1)
        record["history"] = list(record.get("history") or [])
        record["created_at"] = record.get("created_at") or iso_now()
        record["updated_at"] = record.get("updated_at") or record["created_at"]
        record["hits"] = int(record.get("hits") or 0)
        record["source"] = record.get("source") or "rule"
        if kind == "facts":
            record["pinned"] = bool(record.get("pinned"))
            record["tombstone_reason"] = record.get("tombstone_reason")
            record["last_used_at"] = record.get("last_used_at")
        else:
            record.setdefault("event_at", None)
            record.setdefault("due_at", None)
            record.setdefault("last_used_at", None)
            record.setdefault("fact_id", None)
            record.setdefault("tombstone_reason", None)
        return record

    def _load_file(self, kind):
        path = self.facts_path if kind == "facts" else self.episodes_path
        with self._file_locks[kind]:
            stat_key = self._stat_key(path)
            if self._cache[kind] is not None and stat_key == self._cache_stat[kind]:
                return self._cache[kind]
            records = self._read_jsonl(path, kind)
            self._cache[kind] = records
            self._cache_stat[kind] = self._stat_key(path)
            return records

    def _write_file(self, kind, records):
        path = self.facts_path if kind == "facts" else self.episodes_path
        with self._file_locks[kind]:
            try:
                lines = [json.dumps(item, ensure_ascii=False, sort_keys=True)
                         for item in records]
                body = "\n".join(lines)
                if body:
                    body += "\n"
                self._atomic_write(path, body)
            except Exception:
                # 调用方是原地改缓存后再写；写失败必须失效缓存，避免读到未落盘状态。
                self._cache[kind] = None
                self._cache_stat[kind] = None
                raise
            self._cache[kind] = copy.deepcopy(records)
            self._cache_stat[kind] = self._stat_key(path)

    def _reload(self, kind):
        with self._file_locks[kind]:
            self._cache[kind] = None
            self._cache_stat[kind] = None
        return self._load_file(kind)

    # ------------------------------------------------------------------
    # 通用读取
    # ------------------------------------------------------------------
    def facts(self, include_tombstone=True):
        records = copy.deepcopy(self._load_file("facts"))
        if include_tombstone:
            return records
        return [item for item in records if item.get("status") == "active"]

    def episodes(self, include_tombstone=True):
        records = copy.deepcopy(self._load_file("episodes"))
        if include_tombstone:
            return records
        return [item for item in records if item.get("status") == "active"]

    def active_facts(self):
        return self.facts(include_tombstone=False)

    def active_episodes(self):
        return self.episodes(include_tombstone=False)

    def find(self, record_id_value):
        for kind in ("facts", "episodes"):
            for item in self._load_file(kind):
                if item.get("id") == record_id_value:
                    return copy.deepcopy(item)
        return None

    def find_fact(self, record_id_value):
        for item in self._load_file("facts"):
            if item.get("id") == record_id_value:
                return copy.deepcopy(item)
        return None

    def find_episode(self, record_id_value):
        for item in self._load_file("episodes"):
            if item.get("id") == record_id_value:
                return copy.deepcopy(item)
        return None

    def bad_line_counts(self):
        return {"facts": self._bad_lines.get("facts", 0),
                "episodes": self._bad_lines.get("episodes", 0),
                "unknown_kinds": self._unknown_kinds}

    # ------------------------------------------------------------------
    # facts 写入
    # ------------------------------------------------------------------
    def upsert_fact(self, kind, text, slot=None, pinned=False,
                    source="rule", explicit=False, now=None,
                    enforce_capacity=True):
        text = _clean_text(text)
        if not text:
            raise ValueError("fact text 不能为空")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError("fact text 过长（>%d）" % MAX_TEXT_CHARS)
        if kind not in FACT_KINDS:
            raise ValueError("未知 fact kind: %r" % (kind,))
        now_iso = iso_now(self._now(now))
        with self._write_guard():
            records = self._load_file("facts")
            normalized = normalize_text(text)
            new_id = record_id(kind, text)
            target_index = self._find_index(records, new_id)
            if target_index is None and slot:
                target_index = self._find_slot_index(records, slot, explicit=explicit)
            if target_index is not None:
                record = records[target_index]
                if record.get("status") == "tombstone" and not explicit:
                    self.bump_counter("tombstone_ignored", persist=False)
                    return copy.deepcopy(record)
                if record.get("status") == "tombstone" and explicit:
                    history = list(record.get("history") or [])
                    history.append(_new_history_entry(record, now_iso))
                    record.update({
                        "text": text,
                        "slot": slot or record.get("slot"),
                        "pinned": True,
                        "status": "active",
                        "tombstone_reason": None,
                        "revision": int(record.get("revision") or 1) + 1,
                        "history": history,
                        "updated_at": now_iso,
                        "last_used_at": None,
                        "hits": 0,
                        "source": "explicit",
                    })
                    self._write_file("facts", records)
                    if enforce_capacity:
                        self.enforce_capacity()
                    return copy.deepcopy(record)

                if normalize_text(record.get("text")) != normalized:
                    self._replace_text(record, text, now_iso)
                if explicit:
                    record["pinned"] = True
                    record["source"] = "explicit"
                    record["updated_at"] = now_iso
                elif pinned:
                    record["pinned"] = True
                self._write_file("facts", records)
                if enforce_capacity:
                    self.enforce_capacity()
                return copy.deepcopy(record)

            record = make_fact(
                kind, text, slot=slot, pinned=bool(pinned or explicit),
                source="explicit" if explicit else source, now=now_iso,
            )
            records.append(record)
            self._write_file("facts", records)
            if enforce_capacity:
                self.enforce_capacity()
            return copy.deepcopy(record)

    def set_pinned(self, record_id_value, pinned, now=None):
        now_iso = iso_now(self._now(now))
        with self._write_guard():
            records = self._load_file("facts")
            for record in records:
                if record.get("id") != record_id_value:
                    continue
                if record.get("status") != "active":
                    return False
                record["pinned"] = bool(pinned)
                record["updated_at"] = now_iso
                self._write_file("facts", records)
                return True
        return False

    def delete_fact(self, record_id_value, reason="deleted", now=None):
        return self._tombstone("facts", record_id_value, reason, now)

    # ------------------------------------------------------------------
    # episodes 写入
    # ------------------------------------------------------------------
    def upsert_episode(self, kind, text, slot=None, due_at=None,
                       event_at=None, source="rule", now=None,
                       enforce_capacity=True, fact_id=None, revive=False):
        text = _clean_text(text)
        if not text:
            raise ValueError("episode text 不能为空")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError("episode text 过长（>%d）" % MAX_TEXT_CHARS)
        if kind not in EPISODE_KINDS:
            raise ValueError("未知 episode kind: %r" % (kind,))
        now_iso = iso_now(self._now(now))
        due_iso = iso_now(due_at) if due_at is not None else None
        event_iso = iso_now(event_at) if event_at is not None else None
        with self._write_guard():
            records = self._load_file("episodes")
            normalized = normalize_text(text)
            new_id = record_id(kind, text)
            target_index = self._find_index(records, new_id)
            if target_index is None and slot:
                target_index = self._find_slot_index(records, slot, explicit=False)
            if target_index is not None:
                record = records[target_index]
                if record.get("status") == "tombstone":
                    if not revive:
                        self.bump_counter("tombstone_ignored", persist=False)
                        return copy.deepcopy(record)
                    history = list(record.get("history") or [])
                    history.append(_new_history_entry(record, now_iso))
                    record.update({
                        "kind": kind,
                        "text": text,
                        "slot": slot or record.get("slot"),
                        "status": "active",
                        "tombstone_reason": None,
                        "revision": int(record.get("revision") or 1) + 1,
                        "history": history,
                        "updated_at": now_iso,
                        "last_used_at": None,
                        "hits": 0,
                        "source": source,
                        "due_at": due_iso,
                        "event_at": event_iso,
                        "fact_id": fact_id if fact_id is not None
                                   else record.get("fact_id"),
                    })
                    self._write_file("episodes", records)
                    if enforce_capacity:
                        self.enforce_capacity()
                    return copy.deepcopy(record)
                if normalize_text(record.get("text")) != normalized:
                    self._replace_text(record, text, now_iso)
                    record["due_at"] = due_iso
                    record["event_at"] = event_iso
                elif record.get("due_at") is None and due_iso is not None:
                    record["due_at"] = due_iso
                if record.get("event_at") is None and event_iso is not None:
                    record["event_at"] = event_iso
                if fact_id is not None:
                    record["fact_id"] = fact_id
                self._write_file("episodes", records)
                if enforce_capacity:
                    self.enforce_capacity()
                return copy.deepcopy(record)

            record = make_episode(
                kind, text, slot=slot, due_at=due_iso, event_at=event_iso,
                source=source, now=now_iso, fact_id=fact_id,
            )
            records.append(record)
            self._write_file("episodes", records)
            if enforce_capacity:
                self.enforce_capacity()
            return copy.deepcopy(record)

    def delete_episode(self, record_id_value, reason="deleted", now=None):
        return self._tombstone("episodes", record_id_value, reason, now)

    def purge(self, record_id_value):
        removed = 0
        with self._write_guard():
            for kind in ("facts", "episodes"):
                records = self._load_file(kind)
                kept = [item for item in records if item.get("id") != record_id_value]
                if len(kept) != len(records):
                    removed += len(records) - len(kept)
                    self._write_file(kind, kept)
        return removed

    def _tombstone(self, kind, record_id_value, reason, now=None):
        if reason not in TOMBSTONE_REASONS:
            raise ValueError("未知 tombstone_reason: %r" % (reason,))
        now_iso = iso_now(self._now(now))
        with self._write_guard():
            records = self._load_file(kind)
            for record in records:
                if record.get("id") != record_id_value:
                    continue
                if record.get("status") == "tombstone":
                    return False
                history = list(record.get("history") or [])
                history.append(_new_history_entry(record, now_iso))
                record.update({
                    "status": "tombstone",
                    "tombstone_reason": reason,
                    "revision": int(record.get("revision") or 1) + 1,
                    "history": history,
                    "updated_at": now_iso,
                })
                self._write_file(kind, records)
                return True
        return False

    def _replace_text(self, record, text, now_iso):
        history = list(record.get("history") or [])
        history.append(_new_history_entry(record, now_iso))
        record.update({
            "text": text,
            "revision": int(record.get("revision") or 1) + 1,
            "history": history,
            "updated_at": now_iso,
        })

    def _find_index(self, records, record_id_value):
        for index, item in enumerate(records):
            if item.get("id") == record_id_value:
                return index
        return None

    def _find_slot_index(self, records, slot, explicit=False):
        active_index = None
        tombstone_index = None
        for index, item in enumerate(records):
            if not item.get("slot") or item.get("slot") != slot:
                continue
            if item.get("status") == "active":
                active_index = index
                break
            if explicit and item.get("status") == "tombstone" and tombstone_index is None:
                tombstone_index = index
        return active_index if active_index is not None else tombstone_index

    # ------------------------------------------------------------------
    # 命中计数
    # ------------------------------------------------------------------
    def mark_used(self, facts=None, episodes=None, now=None, persist=True):
        fact_ids = set(facts or ())
        episode_ids = set(episodes or ())
        now_iso = iso_now(self._now(now))
        changed = False
        with self._write_guard():
            for kind, ids in (("facts", fact_ids), ("episodes", episode_ids)):
                if not ids:
                    continue
                records = self._load_file(kind)
                hit = False
                for record in records:
                    if record.get("id") in ids and record.get("status") == "active":
                        record["hits"] = int(record.get("hits") or 0) + 1
                        record["last_used_at"] = now_iso
                        hit = True
                if hit:
                    changed = True
                    if persist:
                        self._write_file(kind, records)
                    else:
                        self._cache[kind] = records
        return changed

    # ------------------------------------------------------------------
    # 容量
    # ------------------------------------------------------------------
    def enforce_capacity(self, facts_max=ACTIVE_FACTS_MAX,
                         episodes_max=ACTIVE_EPISODES_MAX):
        now_iso = iso_now(self._now())
        with self._write_guard():
            facts = self._load_file("facts")
            active_facts = [item for item in facts if item.get("status") == "active"]
            if len(active_facts) > facts_max:
                unpinned = [item for item in active_facts if not item.get("pinned")]
                evict = _sort_weakest(unpinned)[:len(active_facts) - facts_max]
                evict_ids = {item.get("id") for item in evict}
                if evict_ids:
                    for record in facts:
                        if record.get("id") in evict_ids and record.get("status") == "active":
                            self._tombstone_in_memory(record, "capacity", now_iso)
                    self._write_file("facts", facts)
                    active_facts = [item for item in facts
                                    if item.get("status") == "active"]
            at_capacity = len(active_facts) > facts_max

            def apply(state):
                state["at_capacity"] = at_capacity

            self.update_state(apply)

            episodes = self._load_file("episodes")
            active_episodes = [item for item in episodes
                               if item.get("status") == "active"]
            if len(active_episodes) > episodes_max:
                evict = _sort_weakest(active_episodes)[
                    :len(active_episodes) - episodes_max]
                evict_ids = {item.get("id") for item in evict}
                for record in episodes:
                    if record.get("id") in evict_ids and record.get("status") == "active":
                        self._tombstone_in_memory(record, "capacity", now_iso)
                self._write_file("episodes", episodes)
        return not at_capacity

    def _tombstone_in_memory(self, record, reason, now_iso):
        history = list(record.get("history") or [])
        history.append(_new_history_entry(record, now_iso))
        record.update({
            "status": "tombstone",
            "tombstone_reason": reason,
            "revision": int(record.get("revision") or 1) + 1,
            "history": history,
            "updated_at": now_iso,
        })

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------
    def _session_lock(self, bucket):
        with self._session_locks_guard:
            lock = self._session_locks.get(bucket)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[bucket] = lock
            return lock

    def session_path(self, session_id):
        sid = str(session_id if session_id is not None else "anon")
        if not sid:
            sid = "anon"
        return os.path.join(self.sessions_dir, session_bucket(sid) + ".json")

    def default_session(self, session_id):
        sid = str(session_id if session_id is not None else "anon")
        if not sid:
            sid = "anon"
        return {
            "session_id": sid,
            "summary": "",
            "sticky_goal": "",
            "sticky_todo_id": None,
            "recent_turns": [],
            "updated_at": None,
            "completed_turns": 0,
        }

    def load_session(self, session_id):
        path = self.session_path(session_id)
        bucket = os.path.basename(path)
        lock = self._session_lock(bucket)
        with lock:
            stat_key = self._stat_key(path)
            cached = self._sessions.get(bucket)
            if cached is not None and stat_key is not None \
                    and self._session_stat.get(bucket) == stat_key:
                return copy.deepcopy(cached)
            record = self.default_session(session_id)
            if stat_key is not None:
                record = self._read_session_file(path, session_id)
                if record is None:
                    backup = self._read_session_file(path + ".bak", session_id)
                    if backup is not None:
                        record = backup
                        self.bump_counter("backup_restored", persist=False)
                    else:
                        record = self.default_session(session_id)
            self._sessions[bucket] = record
            self._session_stat[bucket] = self._stat_key(path)
            return copy.deepcopy(record)

    def _read_session_file(self, path, session_id):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        completed = _parse_completed_turns(raw.get("completed_turns"))
        if completed is None:
            return None
        record = self.default_session(session_id)
        record.update({
            "session_id": str(raw.get("session_id") or session_id or "anon"),
            "summary": _clean_text(raw.get("summary")),
            "sticky_goal": _clean_text(raw.get("sticky_goal")),
            "sticky_todo_id": raw.get("sticky_todo_id"),
            "recent_turns": self._sanitize_turns(raw.get("recent_turns")),
            "updated_at": raw.get("updated_at"),
            "completed_turns": completed,
        })
        return record

    def save_session(self, session_id, session, now=None):
        if self.read_only:
            raise MemoryReadOnly("state schema is newer than this code")
        bucket = session_bucket(session_id)
        path = self.session_path(session_id)
        with self._write_guard():
            with self._session_lock(os.path.basename(path)):
                record = self.default_session(session_id)
                if isinstance(session, dict):
                    completed = _parse_completed_turns(
                        session.get("completed_turns"))
                    record.update({
                        "session_id": str(session.get("session_id") or session_id or "anon"),
                        "summary": _clean_text(session.get("summary")),
                        "sticky_goal": _clean_text(session.get("sticky_goal")),
                        "sticky_todo_id": session.get("sticky_todo_id"),
                        "recent_turns": self._sanitize_turns(session.get("recent_turns")),
                        "completed_turns": 0 if completed is None else completed,
                    })
                record["updated_at"] = iso_now(self._now(now))
                self._ensure_root()
                self._atomic_write_json(path, record)
                self._sessions[bucket] = record
                self._session_stat[bucket] = self._stat_key(path)
                return copy.deepcopy(record)

    def _sanitize_turns(self, turns):
        result = []
        if not isinstance(turns, (list, tuple)):
            return result
        for item in turns:
            if not isinstance(item, dict):
                continue
            user = _clean_text(item.get("user"))
            assistant = _clean_text(item.get("assistant"))
            if not user or not assistant:
                continue
            result.append({
                "user": clip_text(user, 40),
                "assistant": clip_text(assistant, 50),
                "ts": item.get("ts"),
            })
        return result[-2:]

    def set_sticky(self, session_id, text, todo_id=None, now=None):
        session = self.load_session(session_id)
        session["sticky_goal"] = clip_text(text, 20)
        session["sticky_todo_id"] = todo_id
        return self.save_session(session_id, session, now=now)

    def clear_sticky_if(self, session_id, todo_id, now=None):
        session = self.load_session(session_id)
        if session.get("sticky_todo_id") != todo_id:
            return False
        session["sticky_goal"] = ""
        session["sticky_todo_id"] = None
        self.save_session(session_id, session, now=now)
        return True

    def clear_sticky_for_todo(self, todo_id, now=None):
        """删除待办后，清空所有指向它的会话 sticky。返回被清空的 session_id。"""
        cleared = []
        try:
            names = os.listdir(self.sessions_dir)
        except OSError:
            return cleared
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.sessions_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict) or raw.get("sticky_todo_id") != todo_id:
                continue
            session_id = raw.get("session_id")
            if not session_id:
                continue
            if self.clear_sticky_if(session_id, todo_id, now=now):
                cleared.append(session_id)
        return cleared

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_active(self, session_id=None):
        data = {
            "facts": [item for item in self.facts() if item.get("status") == "active"],
            "episodes": [item for item in self.episodes()
                         if item.get("status") == "active"],
        }
        if session_id is not None:
            data["session"] = self.load_session(session_id)
        return data

    def directory_size(self):
        total = 0
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    continue
        return total
