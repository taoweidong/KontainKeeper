"""Store 的 maintenance 域方法（QR29-G 第二轮拆分）：经 Mixin 并入门面，调用点零改动。

方言收敛点（_upsert/_ensure_schema/_column_ddl）不在本文件——它们留在 store.py
基座（AGENTS.md 硬约束：三库方言差异全部收在基座两处，不许散回各域）。
本文件的改动必须是纯搬动：混进任何行为变更即视为违规（拆回）。
"""


import json
import time

from sqlalchemy import and_, case, delete, func, select, update

from .helpers import shards
from .tables import (ONLINE_GRACE, audit, commands, containers, heartbeats,
                     hourly, kv, sessions, updates)


class _StoreMaintenanceMixin:

    async def kv_get(self, k, default=None):
        row = await self._one(select(kv.c.v).where(kv.c.k == k))
        return row["v"] if row else default

    async def kv_set(self, k, v):
        return await self._upsert(kv, {"k": k, "v": str(v)}, ["k"], ["v"])

    async def set_agent_latest(self, info):
        await self.kv_set("agent_latest", json.dumps(info, ensure_ascii=False))

    async def get_agent_latest(self):
        return await self._load_json_kv("agent_latest")

    async def set_agent_prev(self, info):
        await self.kv_set("agent_prev", json.dumps(info, ensure_ascii=False))

    async def get_agent_prev(self):
        return await self._load_json_kv("agent_prev")

    async def _load_json_kv(self, key):
        raw = await self.kv_get(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None
    async def cleanup(self, now=None, raw_days=2, cmd_days=30, hourly_days=90, out_days=7,
                      update_days=90, audit_days=90):
        """存储回收：只增不减的表在这里收敛（修 P1-6）。

        两条不同的保留长度是刻意的：命令状态行要留 30 天（审计可追溯），
        但 4MB 的命令输出跟着留一个月纯属浪费——输出单独按 7 天清，状态行保持完整。
        审计表此前不在回收之列（评审 P0）：ip_rejected / interval_violation 是
        外部来源可控的高频写入，只增不减会被撑到磁盘告急。
        """
        now = int(now or time.time())
        stats = {"hours_aggregated": await self._aggregate_hours(now, raw_days)}
        # 大表分批删：一次 DELETE 掉几十万行会长时间锁表、撑大事务日志，
        # SQLite 上还会顶住 WAL 让前台写入一起变慢。
        stats["heartbeats_deleted"] = await self._delete_batched(
            heartbeats, heartbeats.c.ts < now - raw_days * 86400, heartbeats.c.id)
        stats["hourly_deleted"] = await self._run(
            delete(hourly).where(hourly.c.hour < (now - hourly_days * 86400) // 3600))
        stats["commands_deleted"] = await self._delete_batched(
            commands, commands.c.finished_at < now - cmd_days * 86400, commands.c.id,
            extra=commands.c.finished_at.isnot(None))
        stats["outputs_purged"] = await self._purge_outputs(now, out_days)
        stats["sessions_deleted"] = await self._run(
            delete(sessions).where(sessions.c.expires < now))
        # 台账与命令同型只增不减；升级是低频操作，行数远小于心跳，保留 90 天
        stats["updates_deleted"] = await self._delete_batched(
            updates, updates.c.created_at < now - update_days * 86400, updates.c.id)
        # 审计保留 90 天：够溯源，也不给「持续灌拒绝帧」留无限增长的空间
        stats["audit_deleted"] = await self._delete_batched(
            audit, audit.c.ts < now - audit_days * 86400, audit.c.id)
        # sweeper 漏掉的僵死命令盖成 lost 并补 finished_at：不补时间戳的 lost 行
        # 永远落在上面那条 DELETE 的窗口之外，正是 P1-6。
        stats["commands_lost"] = await self._run(
            update(commands).where(commands.c.status.in_(("sent", "running")))
            .where(commands.c.created_at < now - 3600)
            .values(status="lost", finished_at=now))
        return stats

    async def _key_batches(self, col, cond, batch):
        """分批挑主键的公共骨架（QR29-S17）：`while True: select...limit(batch)` 的
        收敛判据（空批退出 / 短批收尾）只写一遍。

        **现在只剩 MySQL 走这条路**（两段式：先挑回主键再按 IN 动作）——
        PG / SQLite 支持「子查询内 LIMIT」，在 _delete_batched / _purge_outputs
        里走单语句变体，每批省一次往返。前提不变：**动作必须让行退出 cond**
        （删掉或改掉标记列），否则死循环。
        """
        while True:
            rows = await self._all(select(col).where(cond).limit(batch))
            if not rows:
                return
            keys = [r[col.name] for r in rows]
            yield keys
            if len(keys) < batch:
                return

    async def _delete_batched(self, table, where, pk, batch=5000, extra=None):
        """按主键分批删除，每批各自一个事务。

        为什么不用 DELETE ... LIMIT：PostgreSQL 不支持，MySQL 的 LIMIT 又是方言
        专属写法，SQLite 还要编译期开关——选主键是唯一三库通用且不锁全表的做法。

        方言分流（收口在本方法内，别处不许再分叉）：
        - PG / SQLite：**单语句分批**——`DELETE ... WHERE pk IN (SELECT pk ...
          LIMIT batch)`，子查询挑批 + 删除一次往返完成（MySQL 明确不支持
          LIMIT 出现在 IN 子查询里，故两段式仅剩它）；
        - MySQL：先挑后删的两段式（_key_batches）。
        """
        cond = where if extra is None else and_(where, extra)
        if self.dialect in ("postgresql", "sqlite"):
            total = 0
            while True:
                n = await self._run(
                    delete(table).where(
                        pk.in_(select(pk).where(cond).limit(batch))))
                total += n
                if n < batch:
                    return total
        total = 0
        async for keys in self._key_batches(pk, cond, batch):
            total += await self._run(delete(table).where(pk.in_(keys)))
        return total

    async def _purge_outputs(self, now, out_days, batch=2000):
        """清掉超龄命令的输出文本，保留状态行（id/pod/rc/耗时都还在）。

        方言分流同 _delete_batched：PG / SQLite 单语句（UPDATE ... WHERE id IN
        (SELECT ... LIMIT batch)），MySQL 两段式。
        """
        if out_days <= 0:
            return 0
        cutoff = now - out_days * 86400
        cond = and_(commands.c.finished_at.isnot(None), commands.c.finished_at < cutoff,
                    commands.c.out_purged == 0, commands.c.out_b64 != "")
        if self.dialect in ("postgresql", "sqlite"):
            total = 0
            while True:
                n = await self._run(
                    update(commands)
                    .where(commands.c.id.in_(
                        select(commands.c.id).where(cond).limit(batch)))
                    .values(out_b64="", out_tail="", out_chunks=0, out_purged=1))
                total += n
                if n < batch:
                    return total
        total = 0
        async for ids in self._key_batches(commands.c.id, cond, batch):
            total += await self._run(update(commands).where(commands.c.id.in_(ids))
                                     .values(out_b64="", out_tail="", out_chunks=0,
                                             out_purged=1))
        return total

    async def counts(self):
        """可观测面板用的行数与状态分布（C5）。

        不追求单条 SQL：这个接口给运维面板看，几分钟调一次，5 次小查询比一条
        三库方言不通的大 JOIN 好维护。
        """
        total = await self._one(select(func.count().label("n")).select_from(containers))
        online = await self._one(select(func.count().label("n")).select_from(containers)
                                 .where(containers.c.online == 1))
        by_status = {r["status"]: r["n"] for r in await self._all(
            select(commands.c.status.label("status"), func.count().label("n"))
            .group_by(commands.c.status))}
        hb = await self._one(select(func.count().label("n")).select_from(heartbeats))
        hrs = await self._one(select(func.count().label("n")).select_from(hourly))
        return {
            "hosts": {"total": (total or {}).get("n", 0), "online": (online or {}).get("n", 0)},
            "commands": by_status,
            "storage": {"heartbeats": (hb or {}).get("n", 0), "hourly": (hrs or {}).get("n", 0)},
        }
