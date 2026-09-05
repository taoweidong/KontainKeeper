"""异步存储层：SQLAlchemy 2 Core + async engine，一套代码适配 SQLite / PG / MySQL。

KK_DB_URL 选库，缺省回落 KK_DB_PATH 的 SQLite。表定义见 tables.py，工具函数见 helpers.py。
阶段 B 的异步化在这里一并完成——Store 全协程，事件循环不再被数据库拖住。
"""
import base64
import hashlib
import json
import logging
import secrets
import time
from urllib.parse import urlparse

from sqlalchemy import (BigInteger, case, delete, func, insert, select, update)
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from .tables import (MD, ONLINE_GRACE, _CMD_COLS, admins, audit, commands,
                     containers, heartbeats, hourly, kv, revoked_tokens, sessions)
from .helpers import _b64_tail, _pwdf, mask_url, normalize_url

log = logging.getLogger("kk.store")

class Store:
    """异步存储。全部方法是协程——调用方 await，事件循环不再被数据库拖住。"""

    def __init__(self, url):
        # 允许直接给裸文件路径（旧 KK_DB_PATH 用法），内部规整成 async URL
        self.url = normalize_url(url)
        self.dialect = urlparse(self.url).scheme.split("+")[0]
        kwargs = {"pool_pre_ping": True}
        if self.dialect == "sqlite":
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in self.url:
                kwargs["poolclass"] = StaticPool   # 内存库每条新连接都会是空库
        self.engine = create_async_engine(self.url, **kwargs)

    # ---- 建库 ----
    async def setup(self):
        async with self.engine.begin() as conn:
            if self.dialect == "sqlite":
                # WAL 是 SQLite 专属优化：让读不挡写（评审 P0-2 的一半根因）
                for pragma in ("journal_mode=WAL", "busy_timeout=5000", "synchronous=NORMAL"):
                    await conn.exec_driver_sql("PRAGMA " + pragma)
            await conn.run_sync(MD.create_all)
        log.info("storage ready: dialect=%s url=%s", self.dialect, self.safe_url())

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

    # ---- 主机与心跳 ----
    async def upsert_container(self, pod, image, agent_ver, interval):
        now = int(time.time())
        return await self._upsert(
            containers,
            {"pod": pod, "image": image or "", "agent_ver": agent_ver or "",
             "hb_interval": int(interval or 60), "first_seen": now, "last_seen": now,
             "last_metrics": "", "online": 0, "status_ts": 0},
            ["pod"], ["image", "agent_ver", "hb_interval", "last_seen"])

    async def set_online(self, pod, online, ts=None, image="", agent_ver=""):
        """在线真相来自 Broker：上线是 retained status，下线是 LWT 或优雅 stop。"""
        now = int(ts or time.time())
        if not online:
            return await self._run(update(containers).where(containers.c.pod == pod)
                                   .values(online=0, status_ts=now))
        return await self._upsert(
            containers,
            {"pod": pod, "image": image or "", "agent_ver": agent_ver or "",
             "hb_interval": 60, "first_seen": now, "last_seen": now,
             "last_metrics": "", "online": 1, "status_ts": now},
            ["pod"], ["online", "status_ts", "last_seen", "image", "agent_ver"])

    async def touch(self, pod):
        return await self._run(update(containers).where(containers.c.pod == pod)
                               .values(last_seen=int(time.time())))

    async def record_hb(self, pod, msg):
        """指标点用帧内 ts（缺失才回落服务器时间）；last_seen 用服务器时间——
        在线判定看的是「服务端何时收到」，与指标时刻是两回事。"""
        now = int(time.time())
        try:
            ts = int(msg.get("ts") or now)
        except (TypeError, ValueError):
            ts = now
        # 主机时钟不可信：明显越界的时间戳一律回落服务器时间，否则坏点会把曲线拉崩
        if not (now - 86400 <= ts <= now + 300):
            ts = now
        m = msg.get("metrics") or {}
        raw = json.dumps(msg, ensure_ascii=False)
        async with self.engine.begin() as conn:
            await conn.execute(insert(heartbeats).values(
                pod=pod, ts=ts, cpu=m.get("cpu"), mem_mb=m.get("mem_mb"), metrics=raw))
            await conn.execute(update(containers).where(containers.c.pod == pod).values(
                last_seen=now, hb_interval=int(msg.get("interval") or 60), last_metrics=raw))
        return ts

    async def list_containers(self):
        return await self._all(select(containers).order_by(containers.c.last_seen.desc()))

    async def get_container(self, pod):
        return await self._one(select(containers).where(containers.c.pod == pod))

    async def containers_exist(self, pods):
        """批量下发前的存在性校验：一次 IN 查询取代 N 次单查（500 台一次点击）。"""
        pods = list(pods or [])
        found = set()
        for i in range(0, len(pods), 400):      # 分片避开数据库变量数上限
            shard = pods[i:i + 400]
            if not shard:
                continue
            rows = await self._all(select(containers.c.pod)
                                   .where(containers.c.pod.in_(shard)))
            found |= {r["pod"] for r in rows}
        return found

    async def online_count(self):
        row = await self._one(select(func.count().label("n")).select_from(containers)
                              .where(containers.c.online == 1))
        return row["n"] if row else 0

    async def online_set(self, grace=ONLINE_GRACE):
        """一次查回全部在线主机集合。

        列表接口若逐行 is_online，500 台就是 500 次往返——比原来的内存查表还差。
        宽限阈值取各行 3×interval 与 grace 的较大者，用 CASE 表达以保持三库通用。
        """
        now = int(time.time())
        span = case((3 * containers.c.hb_interval > grace, 3 * containers.c.hb_interval),
                    else_=grace)
        rows = await self._all(select(containers.c.pod)
                               .where(containers.c.online == 1)
                               .where(containers.c.status_ts >= now - span))
        return {r["pod"] for r in rows}

    async def is_online(self, pod, grace=ONLINE_GRACE):
        row = await self._one(select(containers.c.online, containers.c.status_ts,
                                      containers.c.hb_interval)
                              .where(containers.c.pod == pod))
        if not row or not row["online"]:
            return False
        # 宽限兜底：Broker 崩溃且没来得及发 LWT 时，retained 的 online 会一直挂着
        span = max(3 * (row["hb_interval"] or 60), grace)
        return int(time.time()) - (row["status_ts"] or 0) < span

    async def mark_stale_offline(self, grace=ONLINE_GRACE):
        """把「声称在线但久无 status 刷新」的主机判离线。

        阈值要按各行 hb_interval 放大：GREATEST/max 是三库差异点，CASE 到处都一样。
        """
        span = case((3 * containers.c.hb_interval > grace, 3 * containers.c.hb_interval),
                    else_=grace)
        return await self._run(
            update(containers)
            .where(containers.c.online == 1)
            .where(containers.c.status_ts < int(time.time()) - span)
            .values(online=0))

    async def metrics_series(self, pod, hours=24):
        since = int(time.time()) - hours * 3600
        if hours <= 24:
            rows = await self._all(select(heartbeats.c.ts, heartbeats.c.cpu,
                                          heartbeats.c.mem_mb)
                                   .where(heartbeats.c.pod == pod)
                                   .where(heartbeats.c.ts >= since)
                                   .order_by(heartbeats.c.ts))
            return rows, "raw"
        rows = await self._all(select((hourly.c.hour * 3600).label("ts"),
                                      hourly.c.cpu_avg.label("cpu"),
                                      hourly.c.mem_avg.label("mem_mb"))
                               .where(hourly.c.pod == pod)
                               .where(hourly.c.hour >= since // 3600)
                               .order_by(hourly.c.hour))
        return rows, "hourly"

    # ---- 命令 ----
    async def create_command(self, pod, kind, argv, timeout, created_by):
        return (await self.create_commands_batch([pod], kind, argv, timeout, created_by))[0]

    async def create_commands_batch(self, pods, kind, argv, timeout, created_by):
        """批量建命令：单事务 + 参数列表（驱动侧走 executemany）。"""
        now = int(time.time())
        ids = ["c-" + secrets.token_hex(6) for _ in pods]
        argv_json = json.dumps(argv, ensure_ascii=False)
        rows = [{"id": cid, "pod": pod, "kind": kind, "argv": argv_json,
                 "timeout": timeout, "status": "pending", "created_by": created_by,
                 "created_at": now, "out_b64": ""} for cid, pod in zip(ids, pods)]
        async with self.engine.begin() as conn:
            await conn.execute(insert(commands), rows)
        return ids

    async def get_command(self, cid):
        return await self._one(select(commands).where(commands.c.id == cid))

    async def list_commands(self, pod=None, limit=100):
        stmt = select(*(commands.c[c] for c in _CMD_COLS), commands.c.out_b64)
        if pod:
            stmt = stmt.where(commands.c.pod == pod)
        rows = await self._all(stmt.order_by(commands.c.created_at.desc()).limit(limit))
        for r in rows:
            r["out_tail"] = _b64_tail(r.pop("out_b64", "") or "")
        return rows

    async def command_output(self, cid, as_text=True):
        """完整输出只在单条查看时解码，不进列表响应。"""
        row = await self._one(select(commands.c.out_b64).where(commands.c.id == cid))
        if row is None:
            return None
        raw = base64.b64decode(row["out_b64"] or "", validate=False)
        if as_text:
            return raw.decode("utf-8", "replace")
        # 二进制原样再包一层 base64 回给调用方，不再被 utf-8/replace 污染（评审 L2）
        return base64.b64encode(raw).decode("ascii")

    async def mark_sent(self, cid):
        """语义 = 已发布给 Broker（QoS1 会排队送达），不代表 Agent 已收到。"""
        return await self._run(update(commands).where(commands.c.id == cid)
                               .where(commands.c.status == "pending")
                               .values(status="sent", sent_at=int(time.time())))

    async def append_result(self, msg, host=None):
        """协议 v2 结果帧。

        输出累加直接在 SQL 里拼 base64 字符串：Agent 的 48KB 分块长度是 3 的整数倍，
        拼接结果仍是整段输出的合法 base64——省掉逐块解码重编码的 O(n²)，
        也不再需要进程内缓存（重启不丢，修 P2-15）。
        """
        cid = msg.get("id")
        if cid is None:
            return None
        chunk = str(msg.get("out_b64") or "")
        now = int(time.time())
        cat = commands.c.out_b64 + chunk          # 表达式级拼接，三库通用
        vals = {"out_b64": cat, "out_chunks": commands.c.out_chunks + 1}
        if msg.get("done"):
            vals.update(status="done", rc=msg.get("rc"),
                        timed_out=1 if msg.get("timed_out") else 0,
                        truncated=1 if (msg.get("truncated") or msg.get("rc") == -3) else 0,
                        elapsed_ms=msg.get("elapsed_ms"), finished_at=now)
        else:
            vals.update(status="running")
        return await self._run(update(commands).where(commands.c.id == cid).values(**vals))

    async def sweep_command_timeouts(self, now=None, slack=30):
        """没有终态的命令收敛掉，前端不再无限转圈。

        COALESCE(sent_at, created_at)：publish 失败停在 pending 的行 sent_at 为 NULL，
        只判 sent_at 会让它们永不被扫——正是评审 P1-6 里 lost 永不清理的同一个坑。
        """
        now = int(now or time.time())
        oldest = func.coalesce(commands.c.sent_at, commands.c.created_at)
        return await self._run(
            update(commands)
            .where(commands.c.status.in_(("pending", "sent", "running")))
            .where(oldest + commands.c.timeout + slack < now)
            .values(status="timeout", finished_at=now))

    # ---- 审计 ----
    async def add_audit(self, actor, action, detail=None):
        await self._run(insert(audit).values(
            actor=str(actor)[:64], action=str(action)[:40],
            detail=json.dumps(detail, ensure_ascii=False) if detail else "",
            ts=int(time.time())))

    async def list_audit(self, limit=200):
        return await self._all(select(audit).order_by(audit.c.id.desc()).limit(limit))

    # ---- 管理员与会话 ----
    async def ensure_admin(self, username, password, force=False):
        """不存在则创建；口令与 env 不一致则轮换（改 KK_ADMIN_PASS 后重启即生效）。"""
        row = await self._one(select(admins).where(admins.c.username == username))
        if row is None:
            salt = secrets.token_hex(16)
            await self._run(insert(admins).values(username=username, salt=salt,
                                                  pw_hash=_pwdf(salt, password),
                                                  created=int(time.time())))
            return True
        if force or not secrets.compare_digest(row["pw_hash"], _pwdf(row["salt"], password)):
            salt = secrets.token_hex(16)
            await self._run(update(admins).where(admins.c.username == username)
                            .values(salt=salt, pw_hash=_pwdf(salt, password)))
            return True
        return False

    async def verify_admin(self, username, password):
        row = await self._one(select(admins).where(admins.c.username == username))
        if not row:
            return False
        return secrets.compare_digest(row["pw_hash"], _pwdf(row["salt"], password))

    async def create_session(self, username, ttl=12 * 3600):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        await self._run(insert(sessions).values(token=token, username=username,
                                                created=now, expires=now + ttl))
        return token

    async def get_session(self, token):
        if not token:
            return None
        row = await self._one(select(sessions).where(sessions.c.token == token))
        if not row or row["expires"] < int(time.time()):
            return None
        return row["username"]

    async def delete_session(self, token):
        return await self._run(delete(sessions).where(sessions.c.token == token))

    async def default_admin_exists(self):
        return await self._one(select(admins.c.username)
                               .where(admins.c.username == "admin")) is not None

    # ---- Agent token 吊销 ----
    async def revoke_token(self, token):
        return await self._upsert(revoked_tokens, {"token": token, "ts": int(time.time())},
                                  ["token"], None)

    async def restore_token(self, token):
        return await self._run(delete(revoked_tokens)
                               .where(revoked_tokens.c.token == token))

    async def is_token_revoked(self, token):
        return await self._one(select(revoked_tokens.c.token)
                               .where(revoked_tokens.c.token == token)) is not None

    async def revoked_tokens(self):
        rows = await self._all(select(revoked_tokens.c.token)
                               .order_by(revoked_tokens.c.ts))
        return [r["token"] for r in rows]

    # ---- KV / Agent 版本清单 ----
    async def kv_get(self, k, default=None):
        row = await self._one(select(kv.c.v).where(kv.c.k == k))
        return row["v"] if row else default

    async def kv_set(self, k, v):
        return await self._upsert(kv, {"k": k, "v": str(v)}, ["k"], ["v"])

    async def set_agent_latest(self, info):
        await self.kv_set("agent_latest", json.dumps(info, ensure_ascii=False))

    async def get_agent_latest(self):
        raw = await self.kv_get("agent_latest")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # ---- 聚合与回收 ----
    async def _aggregate_hours(self, now, raw_days=2):
        """把已结束的小时聚合进 hourly 表并推进水位线。

        窗口必须夹在「保留期内」：早先按 MIN(ts) 定起点，一条坏时钟的 ts=0 心跳
        就能让循环跑上几十万次把服务卡死（record_hb 信任帧内 ts 之后，这条路径
        变成可达）。超出保留期的小时反正会被 cleanup 删掉，不值得聚合。
        """
        now_hour = now // 3600
        floor_hour = (now - (raw_days + 1) * 3600) // 3600
        last = int(await self.kv_get("agg_hour", "0") or 0)
        start = max(last, floor_hour)
        if start >= now_hour:
            return 0
        n = 0
        for h in range(start, now_hour):          # 不含当前未结束的小时
            lo, hi = h * 3600, (h + 1) * 3600
            rows = await self._all(
                select(heartbeats.c.pod.label("pod"),
                       func.count().label("samples"),
                       func.avg(heartbeats.c.cpu).label("cpu_avg"),
                       func.max(heartbeats.c.cpu).label("cpu_max"),
                       func.avg(heartbeats.c.mem_mb).label("mem_avg"),
                       func.max(heartbeats.c.mem_mb).label("mem_max"))
                .where(heartbeats.c.ts >= lo).where(heartbeats.c.ts < hi)
                .group_by(heartbeats.c.pod))
            for r in rows:
                lastm = await self._one(select(heartbeats.c.metrics)
                                        .where(heartbeats.c.pod == r["pod"])
                                        .where(heartbeats.c.ts >= lo)
                                        .where(heartbeats.c.ts < hi)
                                        .order_by(heartbeats.c.ts.desc()).limit(1))
                await self._upsert(hourly, {
                    "pod": r["pod"], "hour": h, "samples": r["samples"],
                    "cpu_avg": r["cpu_avg"], "cpu_max": r["cpu_max"],
                    "mem_avg": r["mem_avg"], "mem_max": r["mem_max"],
                    "last_metrics": lastm["metrics"] if lastm else ""},
                    ["pod", "hour"],
                    ["samples", "cpu_avg", "cpu_max", "mem_avg", "mem_max", "last_metrics"])
            n += 1
        await self.kv_set("agg_hour", now_hour - 1)
        return n

    async def cleanup(self, now=None, raw_days=2, cmd_days=30):
        now = int(now or time.time())
        hours = await self._aggregate_hours(now, raw_days)
        await self._run(
            delete(heartbeats).where(heartbeats.c.ts < now - raw_days * 86400),
            delete(commands).where(commands.c.finished_at.isnot(None))
            .where(commands.c.finished_at < now - cmd_days * 86400),
            delete(sessions).where(sessions.c.expires < now),
            # sweeper 漏掉的僵死命令盖成 lost 并补 finished_at：不补时间戳的 lost 行
            # 永远落在上面那条 DELETE 的窗口之外，正是 P1-6。
            update(commands).where(commands.c.status.in_(("sent", "running")))
            .where(commands.c.created_at < now - 3600)
            .values(status="lost", finished_at=now))
        return {"hours_aggregated": hours}
