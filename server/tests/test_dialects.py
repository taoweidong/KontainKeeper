"""跨方言静态校验：SQLite 实测通过，PG / MySQL 在这里保证「编译得出合法 SQL」。

真实三库跑测需要起了 PG/MySQL 实例（CI 用 docker service 最省事）。这个文件的价值
是把最容易在换库当天炸掉的东西先钉住：
- DDL 能编译（MySQL 不接受 TEXT 主键、TEXT 只有 64KB）
- 大字段在 MySQL 上是 LONGTEXT，否则命令输出会被静默截断
- 三种 upsert 写法都能编译成各自方言的合法语句
- 不带方言分支的查询（CASE 宽限、COALESCE 清扫、base64 字符串拼接）能编译
"""
from sqlalchemy.dialects import mysql, postgresql, sqlite
import os

import sqlalchemy
from sqlalchemy import func, select, update

from kk_server.models.store import (MD, Store, admins, audit, commands, containers,  # noqa: F401
                                    heartbeats, hourly, kv, normalize_url,
                                    sessions, mask_url)
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
    win = os.path.join("C:", os.sep + "tmp", "kk.db")     # C:\tmp\kk.db
    assert normalize_url(win) == "sqlite+aiosqlite:///C:/tmp/kk.db"
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
