"""异步存储层：SQLAlchemy 2 Core + async engine，一套代码适配 SQLite / PG / MySQL。

KK_DB_URL 选库，缺省回落 KK_DB_PATH 的 SQLite。表定义见 tables.py，工具函数见 helpers.py。
Store 是门面（QR29-G 第二轮拆分）：基座只留引擎装配、执行原语与方言收敛点，
业务方法按域住在 _store_*.py 的 Mixin 里（auth/audit/commands/metrics/hosts/
maintenance 六域），调用点零改动。
"""
import os
from urllib.parse import urlparse

from sqlalchemy import insert, text
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

# admins..sessions 这批表对象在本文件已无直接使用，但保持可从
# `kk_server.models.store` 导入的历史路径（test_dialects 等既有契约）
from .tables import (MD, _ADD_COLUMNS, _ADD_INDEXES, admins, audit, commands,
                     containers, heartbeats, hourly, kv, sessions, updates)
from .helpers import mask_url, normalize_url
from ._store_auth import _StoreAuthMixin
from ._store_audit import _StoreAuditMixin
from ._store_commands import _StoreCommandsMixin
from ._store_hosts import DISK_ALERT_PCT, _StoreHostsMixin
from ._store_maintenance import _StoreMaintenanceMixin
from ._store_metrics import _StoreMetricsMixin
from ._store_rbac import _StoreRbacMixin
from ..logsetup import get_logger

log = get_logger("kk.store")

def _engine_kwargs(dialect, url):
    """各方言的引擎参数（Store.__init__ 的方言汇聚点之一，纯函数可测）。

    PG（asyncpg）：
    - 连接池：桥接、API 与 janitor 共用，默认 5+10 对 500 台偏小；
      KK_DB_POOL_SIZE / KK_DB_MAX_OVERFLOW 可调（全部配置走 KK_* 的仓规）；
    - server_settings.application_name：pg_stat_activity 里能认出这是谁。
    - **不传 executemany_mode**：values_plus/batch_plus 是 psycopg 系客户端绑参
      驱动的参数，asyncpg（服务端绑参）create_engine 直接拒绝——2026-10-31
      首次真跑 PG 全量测试时暴露（此前引擎从未在 PG 上实例化过，误写进了
      装配注释）。asyncpg 的批量 INSERT 由 SQLAlchemy 2.0 insertmanyvalues
      自动拼单条多值语句，本就不需要这个开关。
    SQLite：既有 connect_args（QR-S3）原样保留；MySQL：驱动不支持 values_plus
    形态，维持默认。
    """
    kwargs = {"pool_pre_ping": True}
    if dialect == "postgresql":
        kwargs["pool_size"] = int(os.environ.get("KK_DB_POOL_SIZE") or 10)
        kwargs["max_overflow"] = int(
            os.environ.get("KK_DB_MAX_OVERFLOW") or 20)
        kwargs["connect_args"] = {
            "server_settings": {"application_name": "kk-server"}}
    elif dialect == "sqlite":
        # timeout 是 sqlite3.connect 的 busy timeout（秒）：**每条连接**都生效。
        # 此前 PRAGMA busy_timeout 只在 setup() 的那一条连接里执行过，池化新建
        # 连接全部回落 0，并发写下 database is locked 防护形同虚设（QR-S3）。
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 5.0}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool   # 内存库每条新连接都会是空库
    return kwargs


# PG 启动调优（_pg_tune 逐条执行，全部幂等）：
# - 热表 autovacuum：kk_containers 每秒被心跳 UPDATE 数次（500 台 × 60s 间隔），
#   默认 scale factor 0.2（20% 死元组才触发）对这种强度近乎永不清理，500 行的表
#   按比例算更是如此——压到行数级让膨胀可控；kk_heartbeats 是追加+批量删，
#   批删后的死元组同样要更勤的回收。
# - fillfactor：kk_containers 是**更新最热的表**（每帧心跳 upsert 整行摘要），
#   100 的默认填充率让每行更新必然拆页、HOT（同页更新，免索引维护）几乎不触发；
#   压到 80 给页内留更新空间，HOT 比例上去后写放大与索引膨胀同步下降。
# - BRIN 索引：ts 与写入物理相关，时间范围扫描（cleanup 挑批、聚合窗口）用
#   KB 级索引替代 btree 的整棵树；CREATE INDEX IF NOT EXISTS 让存量 PG 库
#   升级即得，无需重建表。kk_audit 同型（只追加、ts 物理相关，保留期清理
#   按批挑 ts < cutoff 的行）。
# - 部分索引 idx_cmd_inflight：kk_commands 无界增长（500 台 × 每日数千条，
#   百万行级），而 sweep_command_timeouts 每 30s 扫一遍「在途」子集——
#   在途行数恒为常数（在途即离开谓词、离开索引），部分索引把它从全表扫
#   变成常数级命中。谓词必须与 sweep 的 status IN (...) 逐字同步。
_PG_TABLE_TUNE = {
    "kk_containers": ("autovacuum_vacuum_scale_factor = 0.05, "
                      "autovacuum_analyze_scale_factor = 0.02, "
                      "fillfactor = 80"),
    "kk_heartbeats": "autovacuum_vacuum_scale_factor = 0.02",
    "kk_commands": "autovacuum_vacuum_scale_factor = 0.05",
}


def _pg_tune_stmts():
    """PG 启动调优语句清单（纯函数，便于 compile 级回归）。"""
    stmts = ["ALTER TABLE %s SET (%s)" % (t, o)
             for t, o in _PG_TABLE_TUNE.items()]
    stmts += [
        "CREATE INDEX IF NOT EXISTS idx_hb_ts_brin "
        "ON kk_heartbeats USING BRIN (ts)",
        "CREATE INDEX IF NOT EXISTS idx_audit_ts_brin "
        "ON kk_audit USING BRIN (ts)",
        "CREATE INDEX IF NOT EXISTS idx_cmd_inflight ON kk_commands (created_at) "
        "WHERE status IN ('pending', 'sent', 'running')",
    ]
    return stmts


def _column_ddl(ddl, dialect):
    """补列 DDL 的方言解析：清单允许 `{"mysql": "LONGTEXT", "*": "TEXT"}`。

    方言差异仍然只出现在补列这一条路径上——清单里写的是数据，展开成 ALTER 的唯一
    出口仍是 `Store._ensure_schema`（AGENTS.md 的方言收敛口径）。写死一家的类型会让
    升级库与新建库同一列两型（QR-S33），而 TEXT 类上的字面量 DEFAULT 更是 MySQL
    直接拒绝的 1101（QR-S30）。
    """
    return ddl.get(dialect, ddl["*"]) if isinstance(ddl, dict) else ddl


class Store(_StoreAuthMixin, _StoreAuditMixin, _StoreCommandsMixin,
            _StoreMetricsMixin, _StoreHostsMixin, _StoreMaintenanceMixin,
            _StoreRbacMixin):
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
        self.engine = create_async_engine(
            self.url, **_engine_kwargs(self.dialect, self.url))

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
            if self.dialect == "postgresql":
                await self._pg_tune(conn)
        # RBAC 种子（幂等）：内置角色/菜单缺则补，管理员改过的授权不重置
        await self.seed_rbac()
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

    async def _pg_tune(self, conn):
        """PG 专属的启动调优（方言汇聚点：与 _ensure_schema 同级，仅 PG 执行）。

        全部是幂等的表级 SET / CREATE INDEX IF NOT EXISTS；表属主即可执行
        （ALTER TABLE SET 不需要超级用户）。失败按迁移语义上抛——调优落不下去
        说明账号或实例形态有问题，带病上线不如启动即知。
        """
        for stmt in _pg_tune_stmts():
            await conn.exec_driver_sql(stmt)
        log.info("pg maintenance tuning applied")

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
        """驱动级逃生舱：仅供测试回拨时间与一次性运维，业务代码请用上面的方法。

        参数经 text() 归一后执行（dict 单发 / list[dict] 批发都行）：:name 绑定
        三方言通吃（asyncpg 翻译成 $n、sqlite3/MySQL 原生 named）。**不能走
        exec_driver_sql**——那是驱动裸 SQL，paramstyle 各家不同（sqlite3 吃
        :name，asyncpg 只吃 $n），同一份测试 SQL 无法跨库（2026-10-31 PG
        首跑暴露）。"""
        from sqlalchemy import text as _text
        async with self.engine.begin() as conn:
            r = await conn.execute(_text(sql), params)
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
