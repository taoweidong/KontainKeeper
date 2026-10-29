"""异步存储层：SQLAlchemy 2 Core + async engine，一套代码适配 SQLite / PG / MySQL。

KK_DB_URL 选库，缺省回落 KK_DB_PATH 的 SQLite。表定义见 tables.py，工具函数见 helpers.py。
阶段 B 的异步化在这里一并完成——Store 全协程，事件循环不再被数据库拖住。
"""
import asyncio
import base64
import json
import secrets
import time
from urllib.parse import urlparse

from sqlalchemy import (BigInteger, and_, case, delete, func, insert, or_,
                        select, text, update)
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from .tables import (MD, ONLINE_GRACE, _ADD_COLUMNS, _ADD_INDEXES, _CMD_COLS,
                     _SUMMARY_COLS, admins, audit, commands, containers,
                     heartbeats, hourly, kv, sessions, updates)
from .helpers import (_PWDF_ITERS_LEGACY, TAIL_B64_CHARS, _b64_tail, _num, _pwdf,
                      clamp, mask_url, normalize_url, shards)
from .version import version_lt as _version_lt
from ._store_auth import _StoreAuthMixin
from ._store_audit import _StoreAuditMixin
from ._store_commands import _StoreCommandsMixin
from ._store_hosts import DISK_ALERT_PCT, _StoreHostsMixin
from ._store_maintenance import _StoreMaintenanceMixin
from ._store_metrics import _StoreMetricsMixin
from ..logsetup import get_logger

log = get_logger("kk.store")

def _column_ddl(ddl, dialect):
    """补列 DDL 的方言解析：清单允许 `{"mysql": "LONGTEXT", "*": "TEXT"}`。

    方言差异仍然只出现在补列这一条路径上——清单里写的是数据，展开成 ALTER 的唯一
    出口仍是 `Store._ensure_schema`（AGENTS.md 的方言收敛口径）。写死一家的类型会让
    升级库与新建库同一列两型（QR-S33），而 TEXT 类上的字面量 DEFAULT 更是 MySQL
    直接拒绝的 1101（QR-S30）。
    """
    return ddl.get(dialect, ddl["*"]) if isinstance(ddl, dict) else ddl


class Store(_StoreAuthMixin, _StoreAuditMixin, _StoreCommandsMixin,
            _StoreMetricsMixin, _StoreHostsMixin, _StoreMaintenanceMixin):
    """异步存储门面（QR29-G 第二轮拆分）。

    基座只留三样东西：引擎装配（__init__/setup/close）、执行原语（_all/_one/
    _run/_upsert/_ensure_schema）、方言收敛点（_column_ddl 与补列路径）——
    AGENTS.md 硬约束「三库方言差异全部收在 _upsert/_ensure_schema 两处」的
    那个「两处」就在这里，不许散回各域。业务方法按域住在 _store_*.py 的
    Mixin 里，调用点（controllers/services）零改动。
    """

    def __init__(self, url):
        # 允许直接给裸文件路径（旧 KK_DB_PATH 用法），内部规整成 async URL
        self.url = normalize_url(url)
        self.dialect = urlparse(self.url).scheme.split("+")[0]
        kwargs = {"pool_pre_ping": True}
        if self.dialect == "sqlite":
            # timeout 是 sqlite3.connect 的 busy timeout（秒）：**每条连接**都生效。
            # 此前 PRAGMA busy_timeout 只在 setup() 的那一条连接里执行过，池化新建
            # 连接全部回落 0，并发写下 database is locked 防护形同虚设（QR-S3）。
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 5.0}
            if ":memory:" in self.url:
                kwargs["poolclass"] = StaticPool   # 内存库每条新连接都会是空库
        self.engine = create_async_engine(self.url, **kwargs)

    # ---- 建库 ----
    async def setup(self):
        async with self.engine.begin() as conn:
            if self.dialect == "sqlite":
                # journal_mode=WAL 落在库文件里是持久的；synchronous 是每连接设置，
                # 这里只覆盖 setup 那条连接（性能取舍，不影响并发正确性——并发正确性
                # 由 __init__ 里 connect_args 的 busy_timeout 按连接保证，QR-S3）。
                # aiosqlite 0.22 的游标无法在 sync connect 事件里驱动，逐连接 PRAGMA
                # 需换驱动或等待上游，暂以 connect_args + 首连尽力而为收口。
                for pragma in ("journal_mode=WAL", "synchronous=NORMAL"):
                    await conn.exec_driver_sql("PRAGMA " + pragma)
            await conn.run_sync(MD.create_all)
            await self._ensure_schema(conn)
        log.info("storage ready: dialect=%s url=%s", self.dialect, self.safe_url())

    async def _ensure_schema(self, conn):
        """给既有库补新增列与缺失索引：create_all 只建表，升级后必须自己 ALTER/CREATE。

        方言差异都收在这一个方法里：列/索引清单查 PRAGMA 还是 information_schema、
        MySQL 要按 DATABASE() 限定、以及占位符统一用 :name（exec_driver_sql 的
        位置参数风格各家不同，qmark / $1 / %s 混用必炸）。
        """
        for table, cols in _ADD_COLUMNS.items():
            existing = await self._table_columns(conn, table)
            for name, ddl in cols:
                if name in existing:
                    continue
                await conn.exec_driver_sql(
                    "ALTER TABLE %s ADD COLUMN %s %s"
                    % (table, name, _column_ddl(ddl, self.dialect)))
                log.info("schema migrated: %s.%s added", table, name)
        for table, indexes in _ADD_INDEXES.items():
            existing_idx = await self._table_indexes(conn, table)
            for name, cols in indexes:
                if name in existing_idx:
                    continue
                await conn.exec_driver_sql(
                    "CREATE INDEX %s ON %s (%s)" % (name, table, ", ".join(cols)))
                log.info("schema migrated: index %s on %s(%s) created",
                         name, table, ", ".join(cols))

    async def _table_columns(self, conn, table):
        if self.dialect == "sqlite":
            rows = (await conn.exec_driver_sql("PRAGMA table_info(%s)" % table)).fetchall()
            return {r[1] for r in rows}
        # MySQL 必须限定 schema，否则同实例里另一个库的同名表会让补列被跳过
        scope = ("DATABASE()" if self.dialect == "mysql" else "current_schema()")
        rows = (await conn.execute(text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = :t AND table_schema = " + scope),
            {"t": table})).fetchall()
        return {r[0] for r in rows}

    async def _table_indexes(self, conn, table):
        if self.dialect == "sqlite":
            rows = (await conn.exec_driver_sql("PRAGMA index_list(%s)" % table)).fetchall()
            return {r[1] for r in rows}
        if self.dialect == "mysql":
            rows = (await conn.execute(text(
                "SELECT index_name FROM information_schema.statistics"
                " WHERE table_name = :t AND table_schema = DATABASE()"),
                {"t": table})).fetchall()
            return {r[0] for r in rows}
        rows = (await conn.execute(text(
            "SELECT indexname FROM pg_indexes"
            " WHERE tablename = :t AND schemaname = current_schema()"),
            {"t": table})).fetchall()
        return {r[0] for r in rows}

    def safe_url(self):
        """日志里绝不打印数据库口令。"""
        return mask_url(self.url)

    async def close(self):
        await self.engine.dispose()

    # ---- 执行 helper ----
    async def _all(self, stmt):
        async with self.engine.connect() as conn:
            return [dict(r._mapping) for r in (await conn.execute(stmt)).fetchall()]

    async def _one(self, stmt):
        rows = await self._all(stmt)
        return rows[0] if rows else None

    async def _run(self, *stmts):
        """写操作统一走一个事务；返回受影响行数（多条时取最后一条有效的）。"""
        async with self.engine.begin() as conn:
            n = 0
            for s in stmts:
                r = await conn.execute(s)
                if r.rowcount not in (None, -1):
                    n = r.rowcount
            return n

    async def exec_sql(self, sql, params=()):
        """驱动级逃生舱：仅供测试回拨时间与一次性运维，业务代码请用上面的方法。"""
        async with self.engine.begin() as conn:
            r = await conn.exec_driver_sql(sql, params)
            return r.rowcount

    async def _upsert(self, table, values, conflict_cols, update_cols):
        """全库唯一的方言分支：三种库的 upsert 语法互不相通。"""
        if self.dialect == "mysql":
            stmt = mysql.insert(table).values(**values)
            if update_cols:
                stmt = stmt.on_duplicate_key_update(
                    **{c: stmt.inserted[c] for c in update_cols})
            else:
                stmt = stmt.prefix_with("IGNORE")   # MySQL 没有 DO NOTHING
        elif self.dialect == "postgresql":
            stmt = postgresql.insert(table).values(**values)
            if update_cols:
                stmt = stmt.on_conflict_do_update(index_elements=conflict_cols,
                                                  set_={c: stmt.excluded[c]
                                                        for c in update_cols})
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
        else:
            stmt = sqlite.insert(table).values(**values)
            if update_cols:
                stmt = stmt.on_conflict_do_update(index_elements=conflict_cols,
                                                  set_={c: stmt.excluded[c]
                                                        for c in update_cols})
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
        return await self._run(stmt)

    # ---- 聚合与回收 ----
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

        前提：**动作必须让行退出 cond**（删掉或改掉标记列），否则死循环——
        两个调用方（_delete_batched / _purge_outputs）都满足。
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
        """按主键分批删除：先挑一批 id 再删，每批各自一个事务。

        为什么不用 DELETE ... LIMIT：PostgreSQL 不支持，MySQL 的 LIMIT 又是方言
        专属写法，SQLite 还要编译期开关——选主键是唯一三库通用且不锁全表的做法。
        """
        cond = where if extra is None else and_(where, extra)
        total = 0
        async for keys in self._key_batches(pk, cond, batch):
            total += await self._run(delete(table).where(pk.in_(keys)))
        return total

    async def _purge_outputs(self, now, out_days, batch=2000):
        """清掉超龄命令的输出文本，保留状态行（id/pod/rc/耗时都还在）。"""
        if out_days <= 0:
            return 0
        cutoff = now - out_days * 86400
        cond = and_(commands.c.finished_at.isnot(None), commands.c.finished_at < cutoff,
                    commands.c.out_purged == 0, commands.c.out_b64 != "")
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
