"""跨方言静态校验：SQLite 实测通过，PG / MySQL 在这里保证「编译得出合法 SQL」。

真实三库跑测需要起了 PG/MySQL 实例（CI 用 docker service 最省事）。这个文件的价值
是把最容易在换库当天炸掉的东西先钉住：
- DDL 能编译（MySQL 不接受 TEXT 主键、TEXT 只有 64KB）
- 大字段在 MySQL 上是 LONGTEXT，否则命令输出会被静默截断
- 三种 upsert 写法都能编译成各自方言的合法语句
- 不带方言分支的查询（CASE 宽限、COALESCE 清扫、base64 字符串拼接）能编译
"""
from sqlalchemy.dialects import mysql, postgresql, sqlite

import sqlalchemy
from sqlalchemy import func, select, update

from kk_server.models.store import (MD, Store, admins, audit, commands, containers,  # noqa: F401
                                    heartbeats, hourly, kv, normalize_url,
                                    sessions, mask_url)
from kk_server.models.tables import _ADD_COLUMNS
from kk_server.models.tables import _ADD_COLUMNS
from sqlalchemy.schema import CreateTable

DIALECTS = {"mysql": mysql.dialect(), "postgresql": postgresql.dialect(),
            "sqlite": sqlite.dialect()}

BIG_TEXTS = {"kk_commands": "out_b64", "kk_heartbeats": "metrics",
             "kk_containers": "last_metrics", "kk_hourly": "last_metrics"}


def test_ddl_compiles_on_all_dialects():
    for name, d in DIALECTS.items():
        for table in MD.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=d))
            assert ddl.strip(), (name, table.name)


def test_mysql_uses_longtext_for_big_columns():
    """MySQL 的 TEXT 上限 64KB：命令输出 base64 最大约 5.6MB，必须 LONGTEXT。"""
    d = DIALECTS["mysql"]
    for tname, col in BIG_TEXTS.items():
        ddl = str(CreateTable(MD.tables[tname]).compile(dialect=d))
        assert "LONGTEXT" in ddl.upper(), (tname, col)


def test_mysql_no_literal_default_on_text_columns():
    """MySQL 明确拒绝 BLOB/TEXT 带字面量 DEFAULT（错误 1101），建表阶段就炸。

    PG / SQLite 都接受 `TEXT DEFAULT ''`，所以这条只有 MySQL 会炸——而本地与 CI 默认
    跑 SQLite，只有真连 MySQL 才暴露（QR-S30：v4 的 labels/caps 就是这么带进来的）。
    """
    for table in MD.tables.values():
        ddl = str(CreateTable(table).compile(dialect=DIALECTS["mysql"]))
        for line in ddl.splitlines():
            up = line.upper()
            assert not ("TEXT" in up and "DEFAULT" in up), \
                "%s.%s 给了 MySQL 不接受的字面量 DEFAULT: %s" % (table.name, up.split()[0], line.strip())


def test_add_columns_types_match_the_model():
    """补列路径写死一家的类型，会让升级库与新库同一列两型（QR-S33）。

    labels/caps 的模型侧是 LONGTEXT 变体：MySQL 补列必须解析出 LONGTEXT，PG/SQLite
    解析出 TEXT，并且任何一家都不许带 DEFAULT（MySQL 的 1101 同样适用于 ALTER）。
    """
    entries = dict(_ADD_COLUMNS["kk_containers"])
    from kk_server.models.store import _column_ddl
    for name in ("labels", "caps"):
        ddl = entries[name]
        assert "LONGTEXT" in _column_ddl(ddl, "mysql").upper(), name
        assert "DEFAULT" not in _column_ddl(ddl, "mysql").upper(), name
        for other in ("postgresql", "sqlite"):
            assert _column_ddl(ddl, other).upper().startswith("TEXT"), (name, other)


def test_mysql_primary_keys_are_bounded_varchar():
    """MySQL 索引键必须定长：主键若是 TEXT 直接建不出表。"""
    for table in MD.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, sqlalchemy.String):
                # MySQL 对 TEXT/BLOB 做主键必须带长度，没长度直接建不出表
                assert getattr(col.type, "length", None), (table.name, col.name, str(col.type))


def test_upsert_compiles_for_every_dialect():
    """Store._upsert 的三条方言分支各自要能编译成合法 SQL。"""
    values = {"pod": "p", "image": "i", "agent_ver": "v", "hb_interval": 60,
              "first_seen": 1, "last_seen": 2, "last_metrics": "", "online": 1,
              "status_ts": 3}
    upd = ["online", "status_ts", "last_seen", "image", "agent_ver"]

    stmt = sqlite.insert(containers).values(**values)
    assert "ON CONFLICT" in str(stmt.on_conflict_do_update(index_elements=["pod"],
                                        set_={c: stmt.excluded[c] for c in upd})
                                .compile(dialect=sqlite.dialect()))

    stmt = postgresql.insert(containers).values(**values)
    sql = str(stmt.on_conflict_do_update(index_elements=["pod"],
                                         set_={c: stmt.excluded[c] for c in upd})
              .compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql and "excluded" in sql

    stmt = mysql.insert(containers).values(**values)
    sql = str(stmt.on_duplicate_key_update(**{c: stmt.inserted[c] for c in upd})
              .compile(dialect=mysql.dialect()))
    assert "ON DUPLICATE KEY UPDATE" in sql.upper()

    # do-nothing 形态（kv 初始化等）
    assert "DO NOTHING" in str(postgresql.insert(kv).values(k="a", v="b")
                               .on_conflict_do_nothing(index_elements=["k"])
                               .compile(dialect=postgresql.dialect())).upper()


def test_portable_expressions_compile_everywhere():
    """不带分支的表达式：三库都得认。CASE 代替 GREATEST 就是这个原因。"""
    span = func.coalesce(commands.c.sent_at, commands.c.created_at)
    stmt = (update(commands)
            .where(commands.c.status.in_(("pending", "sent", "running")))
            .where(span + commands.c.timeout + 30 < 10)
            .values(status="timeout", finished_at=10))
    grace = 180
    stale = (update(containers).where(containers.c.online == 1)
             .where(containers.c.status_ts
                    < 100 - func.cast(func.coalesce(3 * containers.c.hb_interval, grace),
                                      containers.c.status_ts.type)))
    tail = update(commands).where(commands.c.id == "c").values(
        out_b64=commands.c.out_b64 + "AAAA")
    series = select((hourly.c.hour * 3600).label("ts"), hourly.c.cpu_avg.label("cpu"))
    for name, d in DIALECTS.items():
        for stmt_ in (stmt, stale, tail, series):
            compiled = str(stmt_.compile(dialect=d))
            assert compiled.strip(), (name, str(stmt_))
    # base64 拼接在 MySQL 会渲染成 CONCAT，PG/SQLite 用 ||
    assert "CONCAT" in str(tail.compile(dialect=mysql.dialect())).upper()
    assert "||" in str(tail.compile(dialect=postgresql.dialect()))


def test_limit_and_in_clause_compile_everywhere():
    stmt = select(commands.c.id).where(commands.c.pod.in_(["a", "b"])).limit(10)
    for name, d in DIALECTS.items():
        sql = str(stmt.compile(dialect=d))
        assert "LIMIT" in sql.upper() or name == "mysql", name


def test_url_normalization_rules():
    assert normalize_url("kk.db") == "sqlite+aiosqlite:///kk.db"
    # 字面 Windows 路径（正斜杠化 + 盘符保留），与宿主 OS 无关：
    # 用 os.path.join + os.sep 在 Linux 上会被绝对段吞掉盘符（曾假红）。
    assert normalize_url("C:\\tmp\\kk.db") == "sqlite+aiosqlite:///C:/tmp/kk.db"
    assert normalize_url("postgresql://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"
    assert normalize_url("postgres://h/d") == "postgresql+asyncpg://h/d"
    assert normalize_url("mysql://h/d") == "mysql+aiomysql://h/d"
    assert normalize_url("mariadb://h/d") == "mysql+aiomysql://h/d"
    assert normalize_url("sqlite+pysqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    # 已是 async 形式的不能二次污染
    assert normalize_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    assert normalize_url("redis://h") == "redis://h"


def test_safe_url_hides_password():
    u = "postgresql://user:sup3rsecret@db.internal:5432/kk"
    assert mask_url(u) == "postgresql://***@db.internal:5432/kk"
    assert "sup3rsecret" not in mask_url(u) and "user" not in mask_url(u)
    # 文件路径没有凭据段，不能误伤
    assert mask_url("sqlite+aiosqlite:///C:/data/kk.db") == "sqlite+aiosqlite:///C:/data/kk.db"


async def test_sqlite_really_executes_the_upsert_and_offset_paths(tmp_path):
    """SQLite 真执行面（第二轮 C.3）：compile 级测试证明「SQL 写得出来」，
    这条证明「真跑得过去」。超长 detail 截断的真执行已由
    test_qr29_remediation.test_audit_detail_is_capped_not_dropped 覆盖，不重复。

    钉住两条 compile 罩不住的语义：
    1. do-nothing upsert（MySQL 侧等价 INSERT IGNORE）冲突时不覆盖、不报错；
       对照组带 update_cols 的形态必须真的覆盖。
    2. 负 OFFSET 在 SQLite 上宽容执行（视同 0）——底库不挡负数，
       真正的防线是 API 层钳制（commands/containers 控制器），别指望底层报错。
    """
    store = Store(str(tmp_path / "exec.db"))
    await store.setup()
    try:
        await store._upsert(kv, {"k": "greet", "v": "first"}, ["k"], [])
        await store._upsert(kv, {"k": "greet", "v": "second"}, ["k"], [])
        assert await store.kv_get("greet") == "first"
        await store._upsert(kv, {"k": "greet", "v": "third"}, ["k"], ["v"])
        assert await store.kv_get("greet") == "third"

        await store.add_audit("tester", "probe", {"n": 1})
        rows = await store.list_audit(limit=5, offset=-5)
        assert [r["action"] for r in rows] == ["probe"]
    finally:
        await store.close()


# ---- PG 性能适配（第二轮）：编译级回归 ----

def test_delete_batched_pg_single_statement():
    """PG/SQLite 的分批删单语句变体：子查询内 LIMIT 挑批，每批一次往返。

    MySQL 明确不支持 LIMIT 出现在 IN 子查询里（错误 1235），保持先挑后删的
    两段式——分支只许收口在 _delete_batched / _purge_outputs 内。
    """
    from sqlalchemy import delete
    from kk_server.models.tables import heartbeats
    cond = heartbeats.c.ts < 100
    stmt = delete(heartbeats).where(
        heartbeats.c.id.in_(select(heartbeats.c.id).where(cond).limit(5000)))
    pg_sql = str(stmt.compile(dialect=DIALECTS["postgresql"],
                              compile_kwargs={"literal_binds": True}))
    assert "DELETE FROM" in pg_sql and "LIMIT 5000" in pg_sql
    # SQLite 同样吃这条形态（store 运行时与 compile 双保险）
    assert "LIMIT 5000" in str(stmt.compile(dialect=DIALECTS["sqlite"],
                                            compile_kwargs={"literal_binds": True}))
    # MySQL 两段式的第一段（挑批）仍然编译成立
    assert "LIMIT" in str(select(heartbeats.c.id).where(cond).limit(5000)
                          .compile(dialect=DIALECTS["mysql"]))


def test_engine_kwargs_per_dialect(monkeypatch):
    """引擎参数按方言分流（Store.__init__ 的汇聚点，纯函数直接测）。

    PG：values_plus（批量建命令 500 条 → 5 次往返）+ 池参数（KK_DB_POOL_SIZE
    可调）+ application_name；SQLite 保持 QR-S3 的 connect_args；
    两边都不许把对方专属的参数带过去（create_async_engine 对未知 kwargs 会炸）。
    """
    from kk_server.models.store import _engine_kwargs
    monkeypatch.setenv("KK_DB_POOL_SIZE", "13")
    pg = _engine_kwargs("postgresql", "postgresql+asyncpg://u:p@h/kk")
    # asyncpg 不吃 executemany_mode（psycopg 系参数）：批量 INSERT 走 SA 2.0
    # insertmanyvalues 自动拼单条多值，无需开关（2026-10-31 PG 实测改锁）
    assert "executemany_mode" not in pg
    assert pg["pool_size"] == 13 and pg["max_overflow"] == 20
    assert pg["connect_args"]["server_settings"]["application_name"] == "kk-server"
    monkeypatch.delenv("KK_DB_POOL_SIZE")
    sq = _engine_kwargs("sqlite", "sqlite+aiosqlite:///x.db")
    assert sq["connect_args"]["timeout"] == 5.0
    assert "executemany_mode" not in sq and "pool_size" not in sq


def test_pg_tune_stmts_are_idempotent():
    """PG 启动调优语句：全部幂等形态（表级 SET / IF NOT EXISTS），
    覆盖三张热表 + BRIN 索引 + 在途命令部分索引——升级库与新建库都靠
    启动时执行这一份。"""
    from kk_server.models.store import _pg_tune_stmts
    stmts = _pg_tune_stmts()
    tables = {"kk_containers", "kk_heartbeats", "kk_commands"}
    assert {x.split()[2] for x in stmts if x.startswith("ALTER")} == tables
    brin = [x for x in stmts if "BRIN" in x]
    assert len(brin) == 2 and "kk_heartbeats" in brin[0] and "kk_audit" in brin[1]
    assert all("IF NOT EXISTS" in x for x in stmts if "CREATE INDEX" in x)
    # 部分索引的谓词必须与 sweep_command_timeouts 的在途口径逐字同步：
    # 谓词一变，索引就不再被清扫查询命中（优化器按谓词蕴含判定）
    inflight = [x for x in stmts if "idx_cmd_inflight" in x]
    assert len(inflight) == 1
    assert "WHERE status IN ('pending', 'sent', 'running')" in inflight[0]
    # 热更新表给 fillfactor：kk_containers 每帧心跳 upsert，留页内空间让 HOT 生效
    containers = [x for x in stmts if x.startswith("ALTER TABLE kk_containers")][0]
    assert "fillfactor = 80" in containers


def test_heartbeats_brin_index_only_on_pg():
    """BRIN 是 PG 专属索引形态：PG 渲染 USING BRIN，SQLite/MySQL 忽略该 kwargs
    （dialect kwarg 的标准行为），三库建表 DDL 都不能炸。"""
    from sqlalchemy.schema import CreateIndex
    from kk_server.models.tables import heartbeats
    brin = [i for i in heartbeats.indexes if i.name == "idx_hb_ts_brin"]
    assert brin, "模型里必须声明 BRIN 索引"
    pg_ddl = str(CreateIndex(brin[0]).compile(dialect=DIALECTS["postgresql"]))
    assert "USING brin" in pg_ddl
    str(CreateIndex(brin[0]).compile(dialect=DIALECTS["sqlite"]))
    str(CreateIndex(brin[0]).compile(dialect=DIALECTS["mysql"]))
