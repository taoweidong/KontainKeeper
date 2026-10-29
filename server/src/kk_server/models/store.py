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
from ._store_hosts import _StoreHostsMixin
from ._store_maintenance import _StoreMaintenanceMixin
from ._store_metrics import _StoreMetricsMixin
from ..logsetup import get_logger

log = get_logger("kk.store")

# 空 reason 的离线（Broker 补发的 LWT）不覆盖**刚写入的** updating 的宽限窗口（B6.1）。
# execv 替换进程后连接被异常断开，Broker 会立刻补发 LWT（reason 恒为空）；若直接落库，
# 运维看到的仍是「原因未知的离线」，B6.1 白做。窗口取 120s，足以覆盖「重启 → 新进程上线」。
_UPDATING_REASON_GRACE = 120




def _clip(value, n):
    """收敛到列宽：os/kernel 这类字符串由 Agent 自报，超长会在 MySQL 严格模式下报错。"""
    s = str(value or "").strip()
    return s[:n]


# labels/caps 的入库长度封顶（字符）。值来自 Agent 自报的 status 帧，匿名 Broker
# 模型下等同外部输入；4096 远小于 MySQL 补列用的 TEXT(64KB)，两条建表路径的列宽
# 差异（新库 LONGTEXT / 老库 TEXT）因此不再要紧。
_JTEXT_MAX = 4096
_TRUNCATED_KEY = "_truncated"


def _jtext(obj):
    """结构化字段（labels / caps）入库：空对象落空串，前端按「空 = 未上报」处理。

    超限时整对丢弃而不是切字符——被截断的 JSON 前端 parse 不出来，比少几个键更糟；
    丢弃后留 `_truncated` 标记，让「被截断」与「没上报」在库里可辨。
    """
    if not isinstance(obj, dict) or not obj:
        return ""
    budget = _JTEXT_MAX - 20          # 给标记键留位，保证落库长度不超 _JTEXT_MAX
    kept = {}
    used = 2                          # "{}"
    for key, value in obj.items():
        add = len(json.dumps({key: value}, ensure_ascii=False)) + (1 if kept else 0)
        if used + add > budget:
            kept[_TRUNCATED_KEY] = "1"
            break
        kept[key] = value
        used += add
    return json.dumps(kept, ensure_ascii=False)


# v4 机队筛选：主机类型白名单与排序白名单。
# 两者都是「用户输入直接拼进 SQL 的地方」——列名无法参数化，类型枚举不可信，
# 所以宁可在这里显式穷举，也不在控制器里做 if/else 拼条件。
_HOST_TYPES = ("container", "vm", "metal")
_SORTS = ("last_seen", "cpu", "mem_mb", "disk_pct", "pod")
_ORDERS = ("asc", "desc")

# 磁盘告警阈值：筛选、列表标记与导出三处共用同一个数，分家就是「筛出来的与标红的不一致」
DISK_ALERT_PCT = 85


def _host_filters(group=None, host_type=None, docker_unhealthy=None,
                  proto_ver=None, online=None, keyword=None, alert=None):
    """主机筛选条件列表（list_containers 与 count_containers 共用，避免两处漂移）。"""
    conds = []
    if group is not None and str(group).strip():
        conds.append(containers.c.group_name == str(group).strip()[:64])
    if host_type is not None and str(host_type).strip():
        # 不在白名单内的类型直接忽略：静默查不到比 500 更难排查
        ht = str(host_type).strip().lower()
        if ht in _HOST_TYPES:
            conds.append(containers.c.host_type == ht)
    if docker_unhealthy:
        # 只要「有异常容器」这一种语义：0 与未探测（同为 0）在此场景等价
        conds.append(containers.c.docker_unhealthy > 0)
    if alert:
        conds.append(containers.c.disk_pct >= DISK_ALERT_PCT)
    if online is not None:
        # 在线筛选必须与渲染同口径（QR29-S15）：裸 online 列只反映 retained/LWT
        # 的最后一帧，宽限兜底（online_set / is_online）要在 SQL 里用同一 CASE
        # 再现，否则 ?online=true 会返回渲染为离线的行，导出与「所见」不一致。
        span = case((3 * containers.c.hb_interval > ONLINE_GRACE,
                     3 * containers.c.hb_interval), else_=ONLINE_GRACE)
        live = (containers.c.online == 1) & (containers.c.status_ts
                                             >= int(time.time()) - span)
        conds.append(live if online else ~live)
    if proto_ver is not None:
        try:
            conds.append(containers.c.proto_ver == int(proto_ver))
        except (TypeError, ValueError):
            pass
    kw = str(keyword or "").strip()
    if kw:
        # autoescape：用户搜 "100%" 时 % 是字面量而不是通配符
        like = kw[:64]
        conds.append(or_(containers.c.pod.contains(like, autoescape=True),
                        containers.c.image.contains(like, autoescape=True),
                        containers.c.os_name.contains(like, autoescape=True),
                        containers.c.group_name.contains(like, autoescape=True)))
    return conds


def _sort_col(sort):
    """排序字段归一：未知值回落 last_seen（列表的默认语义）。"""
    return sort if sort in _SORTS else "last_seen"


def _sort_dir(sort, order):
    """排序方向归一：认不出的 order 回落到该列的默认方向。

    默认降序（最新 / 最高在前）是运维直觉，唯独 pod 是名单——字典升序才稳定。
    显式方向换来的是「最久没报」「CPU 最低」也能顶到前面，降序只能看热闹。
    """
    o = str(order or "").strip().lower()
    if o in _ORDERS:
        return o
    return "asc" if sort == "pod" else "desc"


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

    # ---- 主机与心跳 ----
    async def upsert_container(self, pod, image, agent_ver, interval):
        now = int(time.time())
        return await self._upsert(
            containers,
            {"pod": pod, "image": image or "", "agent_ver": agent_ver or "",
             "hb_interval": int(interval or 60), "first_seen": now, "last_seen": now,
             "last_metrics": "", "online": 0, "status_ts": 0,
             # labels/caps 没有 server_default（MySQL 不接受 TEXT 的默认值），
             # 未上报统一由写入侧给空串；不放进 update_cols，避免补建时抹掉已有元信息
             "labels": "", "caps": ""},
            ["pod"], ["image", "agent_ver", "hb_interval", "last_seen"])

    async def set_online(self, pod, online, ts=None, image="", agent_ver="",
                         reason="", ip="", env=None, group="", labels=None,
                         caps=None, proto_ver=None, docker=None):
        """在线真相来自 Broker：上线是 retained status，下线是 LWT 或优雅 stop。

        reason 是 Agent 自报的状态原因（B6.1：自更新前自报 reason=updating）。LWT
        的 reason 恒为空且紧随 updating 之后到达，这里对「空 reason 覆盖刚写入的
        updating」做一次宽限保留，否则升级窗口仍会显示成「原因未知的离线」。

        v4 起 status 帧另带主机元信息（env/group/labels/caps/proto_ver/docker 摘要），
        全部**只在上线路径落库**：离线帧可能是 LWT（Broker 在断连后补发，字段可能缺失
        或来自旧版本 Agent），拿旧数据覆盖新元信息只会把机队视图改脏。
        """
        now = int(time.time())
        try:
            ts_n = int(ts) if ts else now
        except (TypeError, ValueError):
            ts_n = now
        # 主机时钟不可信：status 帧的 ts 与 record_hb 同款钳制，越界回落服务器
        # 时间（QR29-S5）。时钟超前的主机若把 status_ts 写进未来，is_online 与
        # mark_stale_offline 的判定式在负差值上恒为「在线」——死机显示在线，
        # 且 retained 状态与 LWT 都被这个值影响，离线兜底永不生效。
        now = ts_n if now - 86400 <= ts_n <= now + 300 else now
        # v4 元信息归一：env 里挑列表/详情要用的标量，坏值一律回落空串
        env = env if isinstance(env, dict) else {}
        meta = {
            "host_type": _clip(env.get("virt") or "container", 16) or "container",
            "os_name": _clip(env.get("os"), 80),
            "kernel": _clip(env.get("kernel"), 80),
            "arch": _clip(env.get("arch"), 20),
            "ip": _clip(ip, 64),
            "group_name": _clip(group, 64),
            "labels": _jtext(labels if isinstance(labels, dict) else {}),
            "caps": _jtext(caps if isinstance(caps, dict) else {}),
        }
        try:
            meta["proto_ver"] = int(proto_ver) if proto_ver is not None else 3
        except (TypeError, ValueError):
            meta["proto_ver"] = 3
        dk = docker if isinstance(docker, dict) else {}
        for key in ("total", "running", "unhealthy"):
            try:
                meta["docker_" + key] = max(0, int(dk.get(key) or 0))
            except (TypeError, ValueError):
                meta["docker_" + key] = 0
        if not online:
            keep = ""
            if not reason:
                row = await self.get_container(pod)
                if (row and row.get("status_reason") == "updating"
                        and int(time.time()) - int(row.get("status_ts") or 0)
                        <= _UPDATING_REASON_GRACE):
                    keep = "updating"
            return await self._run(update(containers).where(containers.c.pod == pod)
                                   .values(online=0, status_ts=now,
                                           status_reason=(reason or keep)))
        return await self._upsert(
            containers,
            {"pod": pod, "image": image or "", "agent_ver": agent_ver or "",
             "hb_interval": 60, "first_seen": now, "last_seen": now,
             "last_metrics": "", "online": 1, "status_ts": now,
             "status_reason": str(reason or "online"), **meta},
            ["pod"], ["online", "status_ts", "last_seen", "image", "agent_ver",
                      "status_reason"] + list(meta.keys()))

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
        # 摘要列随心跳顺手写：列表接口就不用再逐行解析 last_metrics（B6）
        summary = self._summary_of(m)
        # cpu/mem_mb 落 Float 列前先规整：Agent 插件写坏了指标也不能让整帧心跳
        # 因为类型错误丢掉——曲线少一个点可以忍，整帧没了就是数据空洞。
        # ip 只在帧里真报了才覆盖：探测失败是「空串」这一正常形态，拿它写回会把
        # status 帧建立的地址抹成空白，详情页的连接信息在一次心跳后就没内容了。
        vals = {"last_seen": now, "status_ts": now, "last_metrics": raw,
                "hb_interval": int(msg.get("interval") or 60)}
        reported_ip = _clip(msg.get("ip"), 64)
        if reported_ip:
            vals["ip"] = reported_ip
        async with self.engine.begin() as conn:
            await conn.execute(insert(heartbeats).values(
                pod=pod, ts=ts, cpu=_num(m.get("cpu")), mem_mb=_num(m.get("mem_mb")),
                metrics=raw))
            await conn.execute(update(containers).where(containers.c.pod == pod)
                               .values(**vals, **summary))
        return ts

    @staticmethod
    def _summary_of(metrics):
        """从一帧 metrics 里摘出列表要显示的三个标量；坏数据返回 0 而不是让整帧失败。"""
        disks = metrics.get("disks") or {}
        try:
            pcts = [float((d or {}).get("pct") or 0) for d in disks.values()]
        except (AttributeError, TypeError, ValueError):
            pcts = []
        return {"cpu": _num(metrics.get("cpu")), "mem_mb": _num(metrics.get("mem_mb")),
                "disk_pct": max(pcts) if pcts else 0.0}

    async def list_containers(self, view="full", limit=None, offset=None,
                              group=None, host_type=None, docker_unhealthy=None,
                              proto_ver=None, online=None, keyword=None, alert=None,
                              sort="last_seen", order=""):
        """full = 全列（含完整 last_metrics），summary = 只读摘要列。

        摘要视图存在的理由：500 台 × 每帧 2~4KB 的 last_metrics 全量解析是列表接口
        的主要开销，而列表页只显示在线/CPU/内存/磁盘告警几个标量。

        limit/offset 用于 full 视图分页（P2：无上限时 500 台完整指标一次性拉回过量）。

        v4 起筛选全部下推 SQL：分组、主机类型、Docker 异常、协议版本、关键字。
        前端再本地过一遍会让「导出」与「所见」不一致，500 台时还会把整个机队拖进
        浏览器。排序走 _SORTS 白名单——列名无法参数化，裸拼等于开注入口。
        """
        if view not in ("full", "summary"):
            raise ValueError("view 需为 full 或 summary，收到 %r" % view)
        q = (select(*(containers.c[c] for c in _SUMMARY_COLS))
             if view == "summary" else select(containers))
        sort = _sort_col(sort)
        for cond in _host_filters(group=group, host_type=host_type,
                                  docker_unhealthy=docker_unhealthy, alert=alert,
                                  proto_ver=proto_ver, online=online, keyword=keyword):
            q = q.where(cond)
        col = containers.c[sort]
        q = q.order_by(col.asc() if _sort_dir(sort, order) == "asc" else col.desc())
        if limit is not None:
            q = q.limit(int(limit))
        if offset:
            q = q.offset(int(offset))
        return await self._all(q)

    async def count_containers(self, group=None, host_type=None,
                               docker_unhealthy=None, proto_ver=None,
                               online=None, keyword=None, alert=None):
        """筛选后的台数：与 list_containers 共用同一套条件，分页控件才不会说谎。"""
        conds = _host_filters(group=group, host_type=host_type,
                              docker_unhealthy=docker_unhealthy, alert=alert,
                              proto_ver=proto_ver, online=online, keyword=keyword)
        q = select(func.count().label("n")).select_from(containers)
        for cond in conds:
            q = q.where(cond)
        row = await self._one(q)
        return row["n"] if row else 0

    async def agent_versions(self, pods):
        """批量取 `{pod: agent_ver}`（D2.2）。

        批量升级要先判 `already_latest`；逐台 `get_container` 在 500 台时就是 500 次
        查询，与 P1-1 同型的 N+1。分片避开数据库变量数上限。
        """
        pods = list(pods or [])
        out = {}
        for shard in shards(pods):
            rows = await self._all(select(containers.c.pod, containers.c.agent_ver)
                                   .where(containers.c.pod.in_(shard)))
            out.update({r["pod"]: (r["agent_ver"] or "") for r in rows})
        return out

    async def in_flight_pods(self, pods):
        """批量取「有未终结升级」的主机集合（D2.3 的 in_flight 去重，一次查完）。"""
        pods = list(pods or [])
        out = set()
        for shard in shards(pods):
            rows = await self._all(
                select(updates.c.pod).where(updates.c.pod.in_(shard))
                .where(updates.c.status.in_(("pending", "queued"))))
            out.update(r["pod"] for r in rows)
        return out

    async def agent_version_counts(self):
        """版本直方图 `{agent_ver: 台数}`，用于算「落后 N 台」（D1.2/D1.3）。

        GROUP BY 而非逐台取回：500 台逐行比版本与列表接口同价，而版本种类通常个位数。
        """
        rows = await self._all(select(containers.c.agent_ver.label("ver"),
                                      func.count().label("n"))
                               .group_by(containers.c.agent_ver))
        return {(r["ver"] or ""): r["n"] for r in rows}

    async def get_container(self, pod):
        return await self._one(select(containers).where(containers.c.pod == pod))

    async def containers_exist(self, pods):
        """批量下发前的存在性校验：一次 IN 查询取代 N 次单查（500 台一次点击）。"""
        pods = list(pods or [])
        found = set()
        for shard in shards(pods):      # 分片避开数据库变量数上限
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

    # ---- 聚合与回收 ----
    async def _aggregate_hours(self, now, raw_days=2):
        """把已结束的小时聚合进 hourly 表并推进水位线。

        窗口必须夹在「保留期内」：早先按 MIN(ts) 定起点，一条坏时钟的 ts=0 心跳
        就能让循环跑上几十万次把服务卡死（record_hb 信任帧内 ts 之后，这条路径
        变成可达）。超出保留期的小时反正会被 cleanup 删掉，不值得聚合。
        """
        now_hour = now // 3600
        # 追赶窗口与 cleanup 的 raw_days*86400 同一口径（QR29-S9）：此前把「天」
        # 当「小时」用（(raw_days+1)*3600），服务端宕机超过 raw_days+1 小时后，
        # 水位直接跳进 now-3h，中间小时永久缺段、历史曲线不可回补。窗口上限
        # 仍是保留期——超出 raw_days 天的小时反正会被 cleanup 删掉，不值得聚合。
        floor_hour = (now - raw_days * 86400) // 3600
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
                # last_metrics 只保留建行时的空串（QR29-D.3，第二轮）：全仓唯一
                # 读方 metrics_series 只取 cpu/mem 均值，写入侧的逐主机逐小时
                # lastm 反查是聚合路径的 N+1，纯浪费。列留在 schema（NOT NULL
                # 无默认，删键会让三库建行都炸）；未来要消费它必须先恢复写入
                # 并更新 test_qr29_remediation 里的对应锁。
                await self._upsert(hourly, {
                    "pod": r["pod"], "hour": h, "samples": r["samples"],
                    "cpu_avg": r["cpu_avg"], "cpu_max": r["cpu_max"],
                    "mem_avg": r["mem_avg"], "mem_max": r["mem_max"],
                    "last_metrics": ""},
                    ["pod", "hour"],
                    ["samples", "cpu_avg", "cpu_max", "mem_avg", "mem_max"])
            n += 1
        await self.kv_set("agg_hour", now_hour - 1)
        return n

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
