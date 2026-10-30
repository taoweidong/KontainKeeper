"""Store 的 hosts 域方法（QR29-G 第二轮拆分）：经 Mixin 并入门面，调用点零改动。

方言收敛点（_upsert/_ensure_schema/_column_ddl）不在本文件——它们留在 store.py
基座（AGENTS.md 硬约束：三库方言差异全部收在基座两处，不许散回各域）。
本文件的改动必须是纯搬动：混进任何行为变更即视为违规（拆回）。
"""


import json
import time

from sqlalchemy import case, func, insert, or_, select, update

from .helpers import _num, shards
from .tables import (ONLINE_GRACE, _SUMMARY_COLS, containers, heartbeats,
                     updates)


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


class _StoreHostsMixin:

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
        # 趋势页序列列：mem_pct 取采集器原值；disk 取多挂载点最大值。
        # 没有挂载数据（老 Agent / 磁盘采集被关）落 NULL 而不是 0——
        # 趋势图上「没读到」与「用到 0%」必须可分（QR-W16 同一口径），
        # 所以这里不复用 _summary_of 的 0.0 兜底（那是给列表标量语义的）。
        disks = m.get("disks") or {}
        try:
            pcts = [float((d or {}).get("pct") or 0) for d in disks.values()]
        except (AttributeError, TypeError, ValueError):
            pcts = []
        async with self.engine.begin() as conn:
            await conn.execute(insert(heartbeats).values(
                pod=pod, ts=ts, cpu=_num(m.get("cpu")), mem_mb=_num(m.get("mem_mb")),
                mem_pct=_num(m.get("mem_pct")),
                disk_pct=_num(max(pcts)) if pcts else None,
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

    async def set_host_limits(self, pod, mem_limit, disk_limit):
        """单机告警上限（百分比）：None = 清除自定义、回落全局口径。调用方校验取值。"""
        await self._run(update(containers).where(containers.c.pod == pod)
                        .values(mem_limit=mem_limit, disk_limit=disk_limit))

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
