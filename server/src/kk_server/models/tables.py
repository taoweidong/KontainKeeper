"""SQLAlchemy 表定义：SQLite / PostgreSQL / MySQL 三库通用的 Core schema。

从 store.py 拆出——纯声明性，不含任何查询逻辑。表名统一加 kk_ 前缀避免与同库其他应用撞名。
"""
from sqlalchemy import (BigInteger, Column, Float, Index, Integer, MetaData,
                        String, Table, Text)
from sqlalchemy.dialects import mysql

MD = MetaData()

# MySQL 的 TEXT 只有 64KB，而命令输出 base64 后最大约 5.6MB（KK_MAX_OUT_MB=4），
# 不升级成 LONGTEXT 会被静默截断。其余两家原生就是无限长。
_LONGTEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")


def _long_text():
    return _LONGTEXT


containers = Table(
    "kk_containers", MD,
    Column("pod", String(120), primary_key=True),
    Column("image", String(200), nullable=False, server_default=""),
    Column("agent_ver", String(40), nullable=False, server_default=""),
    Column("hb_interval", Integer, nullable=False, server_default="60"),
    Column("first_seen", BigInteger, nullable=False),
    Column("last_seen", BigInteger, nullable=False),
    Column("last_metrics", _long_text(), nullable=False),
    Column("online", Integer, nullable=False, server_default="0"),
    Column("status_ts", BigInteger, nullable=False, server_default="0"),
)

heartbeats = Table(
    "kk_heartbeats", MD,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pod", String(120), nullable=False),
    Column("ts", BigInteger, nullable=False),
    Column("cpu", Float),
    Column("mem_mb", Float),
    Column("metrics", _long_text(), nullable=False),
    Index("idx_hb_pod_ts", "pod", "ts"),
)

hourly = Table(
    "kk_hourly", MD,
    Column("pod", String(120), primary_key=True),
    Column("hour", BigInteger, primary_key=True),
    Column("samples", Integer, nullable=False, server_default="0"),
    Column("cpu_avg", Float), Column("cpu_max", Float),
    Column("mem_avg", Float), Column("mem_max", Float),
    Column("last_metrics", _long_text(), nullable=False),
)

commands = Table(
    "kk_commands", MD,
    Column("id", String(32), primary_key=True),
    Column("pod", String(120), nullable=False),
    Column("kind", String(20), nullable=False, server_default="shell"),
    Column("argv", Text, nullable=False),
    Column("timeout", Integer, nullable=False, server_default="30"),
    Column("status", String(12), nullable=False, server_default="pending"),
    Column("created_by", String(64), nullable=False, server_default=""),
    Column("created_at", BigInteger, nullable=False),
    Column("sent_at", BigInteger),
    Column("finished_at", BigInteger),
    Column("rc", Integer),
    Column("timed_out", Integer, nullable=False, server_default="0"),
    Column("truncated", Integer, nullable=False, server_default="0"),
    Column("elapsed_ms", BigInteger),
    Column("out_b64", _long_text(), nullable=False),
    Column("out_chunks", Integer, nullable=False, server_default="0"),
    Index("idx_cmd_pod", "pod", "created_at"),
)

audit = Table(
    "kk_audit", MD,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor", String(64), nullable=False),
    Column("action", String(40), nullable=False),
    Column("detail", Text, nullable=False),
    Column("ts", BigInteger, nullable=False),
)

admins = Table(
    "kk_admins", MD,
    Column("username", String(64), primary_key=True),
    Column("salt", String(64), nullable=False),
    Column("pw_hash", String(128), nullable=False),
    Column("created", BigInteger, nullable=False),
)

sessions = Table(
    "kk_sessions", MD,
    Column("token", String(64), primary_key=True),
    Column("username", String(64), nullable=False),
    Column("created", BigInteger, nullable=False),
    Column("expires", BigInteger, nullable=False),
)

revoked_tokens = Table(
    "kk_revoked_tokens", MD,
    Column("token", String(128), primary_key=True),
    Column("ts", BigInteger, nullable=False),
)

kv = Table(
    "kk_kv", MD,
    Column("k", String(64), primary_key=True),   # MySQL 不接受 TEXT 主键，必须定长
    Column("v", Text, nullable=False),
)

_CMD_COLS = [c.name for c in commands.columns if c.name != "out_b64"]
ONLINE_GRACE = 180
