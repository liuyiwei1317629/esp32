"""回合结束后的异步记忆写入（阶段 3：默认 dry-run，不落盘）。

契约见 design/2026-10-23-layered-memory-design.md §6：
- 入队时生成不可变 WriteJob 快照，worker 只看快照；
- enqueue 同步做敏感预检并更新 recent_cache，下一轮读路径立即可见；
- 单 daemon worker 串行处理；队列满丢弃，不影响回复；
- dry-run 只抽取/组装/计数，不写 facts/episodes/sessions。
"""

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass

import memory_context
import memory_extract
import memory_store

QUEUE_MAXSIZE = 64
RECENT_TURNS = 2
RECENT_CACHE_SESSIONS = 64
SEEN_GEN_LIMIT = 8192
HIT_FLUSH_TURNS = 5
SHUTDOWN_WAIT_SECONDS = 2.0
MIN_CONFIDENCE = 0.7
TURN_EPISODE_CHARS = 60

_STOP = object()


def is_eligible(job):
    """§6 的入队资格：完整、无错误、非截断、有实际内容。"""
    if not job.completed:
        return False
    if job.error:
        return False
    if job.finish_reason == "length":
        return False
    if not job.done and job.finish_reason != "stop":
        return False
    if not str(job.user or "").strip():
        return False
    if not str(job.assistant or "").strip():
        return False
    return True


@dataclass(frozen=True)
class WriteJob:
    session_id: str
    user: str
    assistant: str
    gen: int
    ts: str
    completed: bool
    error: object
    aborted: bool
    done: bool
    finish_reason: object
    selected_ids: tuple = ()

    @classmethod
    def from_turn(cls, turn, now=None):
        stats = getattr(turn, "stats", None) or {}
        abort = getattr(turn, "abort", None)
        return cls(
            session_id=str(getattr(turn, "session_id", "anon") or "anon"),
            user="" if getattr(turn, "user_text", None) is None
            else str(turn.user_text),
            assistant="" if getattr(turn, "reply_text", None) is None
            else str(turn.reply_text),
            gen=int(getattr(turn, "gen", 0) or 0),
            ts=memory_store.iso_now(now),
            completed=bool(getattr(turn, "completed", False)),
            error=getattr(turn, "error", None),
            aborted=bool(abort.is_set()) if abort is not None else False,
            done=bool(stats.get("done")),
            finish_reason=stats.get("finish_reason"),
            selected_ids=tuple(getattr(turn, "selected_ids", None) or ()),
        )


class RecentCache:
    """进程内每 session 最近 2 轮；worker 落盘前下一轮就能读到。"""

    def __init__(self, limit=RECENT_TURNS,
                 max_sessions=RECENT_CACHE_SESSIONS):
        self.limit = max(1, int(limit))
        self.max_sessions = max(1, int(max_sessions))
        self._lock = threading.Lock()
        self._items = {}

    def record(self, session_id, user, assistant, ts):
        item = {
            "user": memory_store.clip_text(user, 40),
            "assistant": memory_store.clip_text(assistant, 50),
            "ts": ts,
        }
        if not item["user"] or not item["assistant"]:
            return False
        sid = str(session_id or "anon")
        with self._lock:
            turns = self._items.pop(sid, None)
            if turns is None:
                turns = []
            turns.append(item)
            del turns[:-self.limit]
            self._items[sid] = turns
            while len(self._items) > self.max_sessions:
                oldest = next(iter(self._items))
                self._items.pop(oldest, None)
        return True

    def get(self, session_id):
        sid = str(session_id or "anon")
        with self._lock:
            turns = self._items.pop(sid, None)
            if turns is None:
                return []
            self._items[sid] = turns
            return [dict(item) for item in turns]

    def clear(self):
        with self._lock:
            self._items.clear()


class MemoryWriter:
    def __init__(self, store, dry_run=True, clock=None,
                 queue_maxsize=QUEUE_MAXSIZE, debug=False,
                 hit_flush_turns=HIT_FLUSH_TURNS, maintenance=None,
                 seen_gen_limit=SEEN_GEN_LIMIT):
        self.store = store
        self.dry_run = bool(dry_run)
        self.debug = bool(debug)
        self.clock = clock or memory_store.utc_now
        self.hit_flush_turns = max(1, int(hit_flush_turns))
        self.maintenance = maintenance
        self.recent_cache = RecentCache()
        self._queue = queue.Queue(maxsize=max(1, int(queue_maxsize)))
        self._lock = threading.Lock()
        self._idle = threading.Condition()
        self._in_flight = 0
        self._seen_gens = set()
        self._seen_gen_order = deque()
        self.seen_gen_limit = max(1, int(seen_gen_limit))
        self._pending_hits = []
        self._hit_turns = 0
        self._turns_since_maintenance = self._restore_turn_counter()
        self._counters = {}
        self._stopping = False
        self._evaluate_maintenance_at_start()
        self._thread = threading.Thread(target=self._worker,
                                        name="memory-writer", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------
    def enqueue(self, job):
        if not is_eligible(job):
            self._bump("rejected_ineligible")
            return False
        with self._lock:
            duplicate = job.gen in self._seen_gens
            if not duplicate:
                if len(self._seen_gen_order) >= self.seen_gen_limit:
                    self._seen_gens.discard(self._seen_gen_order.popleft())
                self._seen_gen_order.append(job.gen)
                self._seen_gens.add(job.gen)
        if duplicate:
            self._bump("rejected_duplicate")
            return False
        if memory_extract.sensitive_reasons(job.user + "\n" + job.assistant):
            self._bump("rejected_sensitive")
            return False
        self.recent_cache.record(job.session_id, job.user, job.assistant,
                                 job.ts)
        with self._idle:
            self._in_flight += 1
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._idle:
                self._in_flight -= 1
                self._idle.notify_all()
            self._bump("rejected_queue_full")
            return False
        self._bump("accepted")
        return True

    def counters(self):
        with self._lock:
            return dict(self._counters)

    def wait_idle(self, timeout=SHUTDOWN_WAIT_SECONDS):
        with self._idle:
            return self._idle.wait_for(lambda: self._in_flight == 0,
                                       timeout=float(timeout))

    def stop(self, timeout=SHUTDOWN_WAIT_SECONDS):
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        try:
            self._queue.put(_STOP, timeout=float(timeout))
        except queue.Full:
            pass
        self._thread.join(timeout)
        if not self.dry_run:
            self._flush_hits()

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def _restore_turn_counter(self):
        try:
            return max(0, int(self.store.state.get("turns_since_maintenance") or 0))
        except (AttributeError, TypeError, ValueError):
            return 0

    def _maintenance_active(self):
        return (self.maintenance is not None and not self.dry_run
                and not getattr(self.store, "read_only", False))

    def _evaluate_maintenance_at_start(self):
        if self._maintenance_active():
            self._run_maintenance(None)

    def _run_maintenance(self, completed_turns):
        try:
            summary = self.maintenance.maybe_run(
                now=self.clock(), completed_turns=completed_turns)
        except Exception:
            self._bump("maintenance_errors")
            return None
        if summary is None:
            return None
        self._turns_since_maintenance = 0
        self._bump("maintenance_runs")
        if self.debug:
            print("memory: maintenance %s" % (summary,))
        return summary

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------
    def _worker(self):
        while True:
            job = self._queue.get()
            try:
                if job is _STOP:
                    return
                try:
                    self._process(job)
                except Exception:
                    self._bump("write_errors")
            finally:
                self._queue.task_done()
                if job is not _STOP:
                    with self._idle:
                        self._in_flight -= 1
                        self._idle.notify_all()

    def _process(self, job):
        now = memory_store.parse_iso(job.ts) or self.clock()
        if memory_extract.sensitive_reasons(job.user + "\n" + job.assistant):
            self._bump("rejected_sensitive_worker")
            return
        candidates = memory_extract.extract(job.user, now=now)
        candidates = [
            item for item in candidates
            if float(item.get("confidence") or 0.0) >= MIN_CONFIDENCE
            and not memory_extract.has_sensitive(item.get("text"))
        ]

        if self.dry_run or getattr(self.store, "read_only", False):
            todo = next((item for item in candidates
                         if item.get("kind") == "todo"), None)
            if todo is None:
                todo_text = todo_id = None
            else:
                todo_text = todo["text"]
                todo_id = memory_store.record_id("todo", todo_text)
            preview = self._assemble_session(job, todo_text, todo_id, now)
            self._bump("read_only_jobs" if not self.dry_run
                       else "dry_run_jobs")
            self._bump("dry_run_candidates", len(candidates))
            self._bump("dry_run_summary_chars", len(preview["summary"]))
            self._record_hits(job)
            if self.debug:
                print("memory: session=%s dry_run=%s candidates=%d summary=%d"
                      % (job.session_id, self.dry_run, len(candidates),
                         len(preview["summary"])))
            return

        todo_record = self._write_candidates(candidates, now)
        session = self._assemble_session(
            job,
            todo_record.get("text") if todo_record else None,
            todo_record.get("id") if todo_record else None,
            now,
        )
        self.store.save_session(job.session_id, session, now=now)
        self._write_turn_episode(job, now)
        self._bump("written_jobs")
        self._record_hits(job)
        if self._maintenance_active():
            self._turns_since_maintenance += 1
            self._run_maintenance(self._turns_since_maintenance)
        if self.debug:
            print("memory: session=%s written candidates=%d"
                  % (job.session_id, len(candidates)))

    def _write_candidates(self, candidates, now):
        todo_record = None
        for candidate in candidates:
            kind = candidate.get("kind")
            try:
                if kind in ("fact", "name", "residence", "relation"):
                    self.store.upsert_fact(
                        kind, candidate["text"], slot=candidate.get("slot"),
                        pinned=bool(candidate.get("pinned")),
                        source=candidate.get("source") or "rule",
                        explicit=candidate.get("source") == "explicit",
                        now=now,
                    )
                elif kind in ("preference", "todo"):
                    record = self.store.upsert_episode(
                        kind, candidate["text"], slot=candidate.get("slot"),
                        due_at=candidate.get("due_at"), source="rule",
                        now=now,
                    )
                    if kind == "todo" and record.get("status") == "active":
                        todo_record = record
            except Exception:
                self._bump("write_errors")
        return todo_record

    def _write_turn_episode(self, job, now):
        """每一轮完整对话落一条 event，构成五层记忆里的「经历」层。"""
        user = memory_store.clip_text(job.user, TURN_EPISODE_CHARS)
        assistant = memory_store.clip_text(job.assistant, TURN_EPISODE_CHARS)
        if not user or not assistant:
            return None
        try:
            return self.store.upsert_episode(
                "event", "用户：%s ／ 我：%s" % (user, assistant),
                source="turn", event_at=now, now=now)
        except Exception:
            self._bump("write_errors")
            return None

    def _assemble_session(self, job, todo_text, todo_id, now):
        session = self.store.load_session(job.session_id)
        sticky = session.get("sticky_goal") or ""
        sticky_id = session.get("sticky_todo_id")
        if todo_text:
            sticky = todo_text
            sticky_id = todo_id
        summary = memory_context.build_summary_storage(
            memory_extract.clean_intent(job.user, 28),
            memory_extract.first_sentence(job.assistant, 28),
            session.get("summary") or "",
            sticky,
        )
        recent = memory_context.merge_recent_turns(
            session.get("recent_turns"),
            self.recent_cache.get(job.session_id),
            RECENT_TURNS,
        )
        session.update({
            "summary": summary["summary"],
            "sticky_goal": summary["sticky_goal"],
            "sticky_todo_id": sticky_id if summary["sticky_goal"] else None,
            "recent_turns": recent,
            "completed_turns": int(session.get("completed_turns") or 0) + 1,
        })
        return session

    # ------------------------------------------------------------------
    # 命中
    # ------------------------------------------------------------------
    def _record_hits(self, job):
        ids = tuple(dict.fromkeys(
            str(value) for value in job.selected_ids if value))
        if not ids:
            return
        if self.dry_run:
            self._bump("hits_deferred", len(ids))
            return
        with self._lock:
            self._pending_hits.append(ids)
            self._hit_turns += 1
            flush = self._hit_turns >= self.hit_flush_turns
        if flush:
            self._flush_hits()

    def _flush_hits(self):
        with self._lock:
            pending = self._pending_hits
            self._pending_hits = []
            self._hit_turns = 0
        if not pending or getattr(self.store, "read_only", False):
            return
        try:
            for index, ids in enumerate(pending):
                self.store.mark_used(
                    facts=ids, episodes=ids, now=self.clock(),
                    persist=(index == len(pending) - 1),
                )
            self._bump("hits_flushes")
        except Exception:
            self._bump("hit_errors")

    def _bump(self, name, amount=1):
        with self._lock:
            self._counters[name] = int(self._counters.get(name) or 0) + int(amount)
