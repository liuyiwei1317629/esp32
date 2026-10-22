"""记忆巩固与维护（纯标准库）。

启动时 / 每 24 h / 每 50 个完整回合触发：
- preference episode 被 L3 选中累计 hits>=3 -> 晋升 fact（source=promoted，不 pin），
  原 episode 置 tombstone（promoted）。
- 未 pin fact：30 天未用 -> 建 demoted episode 后 tombstone；180 天 -> expired。
- episode 180 天无命中 -> tombstone；tombstone 满 1 年物理清理。
- 容量：facts>200 只淘汰 unpinned；pinned 超限 -> at_capacity；episodes>500 淘汰最弱。
- 幂等；state.json 记录 last_maintenance / turns_since_maintenance。

不联网、不调用模型、不占 GPU。
"""

from datetime import datetime, timedelta, timezone

import memory_store

PROMOTE_HITS = 3
FACT_DEMOTE_DAYS = 30
FACT_EXPIRE_DAYS = 180
EPISODE_EXPIRE_DAYS = 180
TOMBSTONE_PURGE_DAYS = 365
MAINTENANCE_INTERVAL_HOURS = 24
MAINTENANCE_TURN_INTERVAL = 50


def _as_utc(value):
    return memory_store.as_utc(value)


def _now(now=None, clock=None):
    if now is not None:
        return _as_utc(now) or memory_store.utc_now()
    if clock is not None:
        return _as_utc(clock()) or memory_store.utc_now()
    return memory_store.utc_now()


def _age_days(timestamp, now):
    ts = _as_utc(timestamp)
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds() / 86400.0)


class MemoryMaintenance:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or memory_store.utc_now

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------
    def maybe_run(self, now=None, completed_turns=None):
        """到 24 h 或距上次维护满 50 个完整回合才跑；返回 run 结果或 None。

        completed_turns 是“距上次维护的增量”，不是会话累计值；调用方应传
        上次维护后新增的完成回合数，传累计值会导致 50 回合后每轮触发维护。
        """
        now_value = _now(now, self.clock)
        state = self.store.state
        if completed_turns is not None:
            state["turns_since_maintenance"] = int(completed_turns)
        last = _as_utc(state.get("last_maintenance"))
        due_time = last is None or \
            (now_value - last) >= timedelta(hours=MAINTENANCE_INTERVAL_HOURS)
        due_turns = int(state.get("turns_since_maintenance") or 0) \
            >= MAINTENANCE_TURN_INTERVAL
        if not (due_time or due_turns):
            if completed_turns is not None and not self.store.read_only:
                self.store.save_state(state)
            return None
        return self.run(now=now_value, turns_since_maintenance=0)

    def run(self, now=None, turns_since_maintenance=0):
        now_value = _now(now, self.clock)
        summary = {
            "promoted": 0,
            "demoted": 0,
            "expired_facts": 0,
            "expired_episodes": 0,
            "purged": 0,
            "capacity_evicted": 0,
            "at_capacity": bool(self.store.state.get("at_capacity")),
        }
        if self.store.read_only:
            summary["read_only"] = True
            return summary

        if not self.store.state.get("at_capacity"):
            summary["promoted"] = self._promote_preferences(now_value)
        demoted, expired = self._demote_facts(now_value)
        summary["demoted"] = demoted
        summary["expired_facts"] = expired
        expired_eps, purged = self._cleanup_episodes(now_value)
        summary["expired_episodes"] = expired_eps
        summary["purged"] = purged

        self.store.enforce_capacity()
        summary["at_capacity"] = bool(self.store.state.get("at_capacity"))

        state = self.store.state
        state["last_maintenance"] = now_value.isoformat(timespec="seconds")
        state["turns_since_maintenance"] = int(turns_since_maintenance or 0)
        self.store.save_state(state)
        return summary

    # ------------------------------------------------------------------
    # 晋升
    # ------------------------------------------------------------------
    def _promote_preferences(self, now):
        promoted = 0
        for episode in self.store.active_episodes():
            if episode.get("kind") != "preference":
                continue
            if int(episode.get("hits") or 0) < PROMOTE_HITS:
                continue
            fact = self.store.upsert_fact(
                "preference",
                episode.get("text", ""),
                slot=episode.get("slot"),
                pinned=False,
                source="promoted",
                now=now,
            )
            if fact.get("status") != "active":
                # 同名 fact 仍是 tombstone（如 demoted）时 upsert 不会复活；
                # 此时删 episode 会让内容从所有活跃层消失，保留待显式处理。
                continue
            if self.store.delete_episode(episode.get("id"), reason="promoted",
                                         now=now):
                promoted += 1
        return promoted

    # ------------------------------------------------------------------
    # 降级 / 过期
    # ------------------------------------------------------------------
    def _demote_facts(self, now):
        demoted = 0
        expired = 0
        existing_demoted = {
            item.get("fact_id") for item in self.store.episodes()
            if item.get("source") == "demoted" and item.get("fact_id")
        }
        for fact in self.store.active_facts():
            if fact.get("pinned"):
                continue
            since = fact.get("last_used_at") or fact.get("created_at")
            age = _age_days(since, now)
            if age is None:
                continue
            if age > FACT_EXPIRE_DAYS:
                if self.store.delete_fact(fact.get("id"), reason="expired",
                                          now=now):
                    expired += 1
                continue
            if age <= FACT_DEMOTE_DAYS:
                continue
            if fact.get("id") in existing_demoted:
                # 上一次在“建 episode”和“tombstone fact”之间中断；补最后一步。
                if self.store.delete_fact(fact.get("id"), reason="demoted",
                                          now=now):
                    demoted += 1
                continue
            kind = fact.get("kind")
            if kind not in memory_store.EPISODE_KINDS:
                kind = "fact"
            self.store.upsert_episode(
                kind,
                fact.get("text", ""),
                slot=fact.get("slot"),
                source="demoted",
                now=now,
                fact_id=fact.get("id"),
                revive=True,
            )
            if self.store.delete_fact(fact.get("id"), reason="demoted",
                                      now=now):
                demoted += 1
        return demoted, expired

    # ------------------------------------------------------------------
    # episode 清理 / tombstone 物理删除
    # ------------------------------------------------------------------
    def _cleanup_episodes(self, now):
        expired = 0
        purged = 0
        for episode in self.store.active_episodes():
            since = episode.get("last_used_at") or episode.get("created_at")
            age = _age_days(since, now)
            if age is not None and age > EPISODE_EXPIRE_DAYS:
                if self.store.delete_episode(episode.get("id"),
                                             reason="expired", now=now):
                    expired += 1
        for kind in ("facts", "episodes"):
            records = self.store.facts() if kind == "facts" \
                else self.store.episodes()
            for record in records:
                if record.get("status") != "tombstone":
                    continue
                age = _age_days(record.get("updated_at"), now)
                if age is not None and age > TOMBSTONE_PURGE_DAYS:
                    purged += self.store.purge(record.get("id"))
        return expired, purged
