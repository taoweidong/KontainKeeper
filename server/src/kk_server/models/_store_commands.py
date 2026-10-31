"""Store 的 commands 域方法（QR29-G 第二轮拆分）：经 Mixin 并入门面，调用点零改动。

方言收敛点（_upsert/_ensure_schema/_column_ddl）不在本文件——它们留在 store.py
基座（AGENTS.md 硬约束：三库方言差异全部收在基座两处，不许散回各域）。
本文件的改动必须是纯搬动：混进任何行为变更即视为违规（拆回）。
"""


import asyncio
import base64
import json
import secrets
import time

from sqlalchemy import and_, case, delete, func, insert, or_, select, update

from .helpers import TAIL_B64_CHARS, _b64_tail, shards
from .version import version_lt as _version_lt
from .tables import _CMD_COLS, commands, updates


class _StoreCommandsMixin:

    # ---- 命令 ----
    async def create_command(self, pod, kind, argv, timeout, created_by):
        return (await self.create_commands_batch([pod], kind, argv, timeout, created_by))[0][0]

    async def create_commands_batch(self, pods, kind, argv, timeout, created_by,
                                    batch_id=None):
        """批量建命令：单事务 + 参数列表（驱动侧走 executemany）。

        返回 `(ids, batch_id)`：一次调用一个批次，全部行共享同一个 batch_id。
        显式返回元组而不是挂在 self 上的隐式状态——批量下发后要按批次聚合核验，
        调用方（控制器）必须拿到这个号，隐式状态在并发下会串。
        """
        now = int(time.time())
        ids = ["c-" + secrets.token_hex(6) for _ in pods]
        batch_id = batch_id or ("b-" + secrets.token_hex(8))
        argv_json = json.dumps(argv, ensure_ascii=False)
        rows = [{"id": cid, "pod": pod, "kind": kind, "argv": argv_json,
                 "timeout": timeout, "status": "pending", "created_by": created_by,
                 "created_at": now, "out_b64": "", "out_tail": "", "batch_id": batch_id}
                for cid, pod in zip(ids, pods)]
        async with self.engine.begin() as conn:
            await conn.execute(insert(commands), rows)
        return ids, batch_id

    async def get_command(self, cid):
        return await self._one(select(commands).where(commands.c.id == cid))

    @staticmethod
    def _command_filters(pod=None, batch=None, status=None, kind=None,
                         since=None, until=None, keyword=None):
        """命令筛选条件的唯一构造点：列表、计数、导出三处共用同一套语义。

        集中在这里的理由：导出结果必须与页面所见一致（P0-1 验收项），
        两处各写一份 where 迟早漂移。
        """
        conds = []
        if pod:
            conds.append(commands.c.pod == pod)
        if batch:
            conds.append(commands.c.batch_id == batch)
        if status:
            conds.append(commands.c.status == status)
        if kind:
            conds.append(commands.c.kind == kind)
        if since:
            conds.append(commands.c.created_at >= int(since))
        if until:
            conds.append(commands.c.created_at <= int(until))
        if keyword:
            # 关键字必须下推到后端：前端过滤会让「导出」与「所见」不一致。
            # autoescape 对齐 _host_filters 的既有写法：用户搜 "100%" 时 %
            # 是字面量而不是通配符，也不再吃 "_" 通配与尾随 % 的慢扫描（QR29-S18）
            kw = str(keyword)[:64]
            conds.append(or_(commands.c.pod.contains(kw, autoescape=True),
                             commands.c.id.contains(kw, autoescape=True),
                             commands.c.argv.contains(kw, autoescape=True)))
        return conds

    async def list_commands(self, pod=None, limit=100, offset=None, batch=None,
                            status=None, kind=None, since=None, until=None,
                            keyword=None, tail=True):
        """只查小列：输出尾段读 out_tail 镜像列（QR-S1）。

        此前无条件把 out_b64 整列拖回（单行最大 ~5.6MB，500 行列表最坏 GB 级
        DB→应用传输），tail=False 时取回即丢。历史行（补列前写入）out_tail 为空，
        前端显示为无预览，新命令即时生效。
        """
        stmt = select(*(commands.c[c] for c in _CMD_COLS))
        for cond in self._command_filters(pod, batch, status, kind, since, until, keyword):
            stmt = stmt.where(cond)
        stmt = stmt.order_by(commands.c.created_at.desc()).limit(limit)
        if offset:
            stmt = stmt.offset(int(offset))
        rows = await self._all(stmt)
        for r in rows:
            raw = r.pop("out_tail", "") or ""
            if tail:
                r["out_tail"] = _b64_tail(raw)
        return rows

    async def count_commands(self, pod=None, batch=None, status=None, kind=None,
                             since=None, until=None, keyword=None):
        """与 list_commands 同筛选条件的总数：分页要 total，导出要行数上限判断。"""
        stmt = select(func.count().label("n")).select_from(commands)
        for cond in self._command_filters(pod, batch, status, kind, since, until, keyword):
            stmt = stmt.where(cond)
        row = await self._one(stmt)
        return row["n"] if row else 0

    async def batch_summary(self, limit=20):
        """最近批次的各自状态分布：让「500 台一次点击」变成可核验的对象。

        一条 GROUP BY + 内存聚合，不按批次循环查——500 台规模下批次可能上百个。
        """
        # ORDER BY 必须落在聚合列上：裸引用 created_at 在 PG 是 GroupingError
        # （SQL 标准拒绝、SQLite/MySQL 宽容放行——2026-10-31 PG 首跑暴露）
        latest_at = func.max(commands.c.created_at).label("created_at")
        rows = await self._all(
            select(commands.c.batch_id.label("batch_id"),
                   commands.c.status.label("status"),
                   func.count().label("n"),
                   latest_at)
            .where(commands.c.batch_id != "")
            .group_by(commands.c.batch_id, commands.c.status)
            .order_by(latest_at.desc()))
        agg = {}
        for r in rows:
            b = agg.setdefault(r["batch_id"], {"batch_id": r["batch_id"], "total": 0,
                                               "created_at": r["created_at"] or 0})
            b[r["status"]] = b.get(r["status"], 0) + r["n"]
            b["total"] += r["n"]
        out = sorted(agg.values(), key=lambda x: x["created_at"], reverse=True)
        return out[:limit] if limit else out

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
        return await self.mark_sent_batch([cid])

    async def mark_sent_batch(self, cids):
        """批量置 sent：500 台一次 UPDATE ... WHERE id IN (...)。

        逐条 UPDATE 在 SQLite 上约数十毫秒，但 500 条是 500 次事务往返——
        批量下发是「一次点击」的路径，这里省下来的都是点击后的等待。
        分片是为了避开数据库变量数上限（与 containers_exist 同款）。
        """
        cids = [c for c in (cids or []) if c]
        if not cids:
            return 0
        now = int(time.time())
        total = 0
        for shard in shards(cids):
            total += await self._run(
                update(commands)
                .where(commands.c.id.in_(shard))
                .where(commands.c.status == "pending")
                .values(status="sent", sent_at=now))
        return total

    async def append_result(self, msg, host=None):
        """协议 v2 结果帧。

        输出累加直接在 SQL 里拼 base64 字符串：Agent 的 48KB 分块长度是 3 的整数倍，
        拼接结果仍是整段输出的合法 base64——省掉逐块解码重编码的 O(n²)，
        也不再需要进程内缓存（重启不丢，修 P2-15）。

        QoS1「至少一次」：Broker 重投时同一 seq 块会被重复拼接，导致命令输出翻倍。
        以 last_seq 为水位做幂等去重（只应用严格更大的 seq）；无 seq 的帧回退为无条件追加。

        out_tail 镜像列（QR-S1）随块维护：取「旧尾 + 本块」的末 TAIL_B64_CHARS 个
        字符（4 的倍数，右截仍是合法 base64）。水位判定先读小列（out_tail/last_seq）
        再定字面量，避免大列参与 SQL CASE；同一 cid 的并发由桥接的 per-cid 锁串行，
        多实例部署下最坏只是尾段预览短暂缺一块，大字段本身的拼接仍是单语句原子。
        """
        cid = msg.get("id")
        if cid is None:
            return None
        chunk = str(msg.get("out_b64") or "")
        seq = msg.get("seq")
        now = int(time.time())
        # seq 规整成 int；坏值按「无水位帧」处理，与旧帧同语义（无条件追加）
        try:
            seq_n = int(seq) if seq is not None else None
        except (TypeError, ValueError):
            seq_n = None
        row = await self._one(select(commands.c.out_tail, commands.c.last_seq)
                              .where(commands.c.id == cid))
        if row is None or row.get("last_seq") is None:
            last_seq_val = -1
        else:
            last_seq_val = int(row["last_seq"])
        applied = True if seq_n is None else last_seq_val < seq_n
        tail = (row or {}).get("out_tail") or ""
        merged = tail + chunk if applied else tail
        if len(merged) > TAIL_B64_CHARS:
            merged = merged[-TAIL_B64_CHARS:]
        if seq_n is None:
            # 老/畸形帧：无 seq 无法去重，保持原语义无条件拼接
            cat = commands.c.out_b64 + chunk          # 表达式级拼接，三库通用
            vals = {"out_b64": cat, "out_chunks": commands.c.out_chunks + 1,
                    "out_tail": merged}
        else:
            # 幂等路径：仅当 seq 严格大于已应用水位才拼接，避免重投翻倍
            applied_cond = commands.c.last_seq < seq
            cat = case((applied_cond, commands.c.out_b64 + chunk), else_=commands.c.out_b64)
            chunks = case((applied_cond, commands.c.out_chunks + 1), else_=commands.c.out_chunks)
            last_seq_v = case((applied_cond, seq), else_=commands.c.last_seq)
            vals = {"out_b64": cat, "out_chunks": chunks, "last_seq": last_seq_v,
                    "out_tail": case((applied_cond, merged), else_=commands.c.out_tail)}
        if msg.get("done"):
            vals.update(status="done", rc=msg.get("rc"),
                        timed_out=1 if msg.get("timed_out") else 0,
                        truncated=1 if (msg.get("truncated") or msg.get("rc") == -3) else 0,
                        elapsed_ms=msg.get("elapsed_ms"), finished_at=now)
        elif seq_n is None:
            vals.update(status="running")
        else:
            # 幂等路径下，迟到重投的非终态分块不能把已终态的命令拉回 running
            # （否则 sweep 超时器随后会把它错误收敛成 timeout）。
            vals["status"] = case((applied, "running"), else_=commands.c.status)
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

    # ---- 自更新台账（A6.2）----
    async def create_update(self, uid, pod, from_version, to_version, status="pending"):
        await self._run(insert(updates).values(
            id=uid, pod=pod, from_version=from_version or "", to_version=to_version or "",
            status=status, reason="", created_at=int(time.time()), finished_at=None))
        return uid

    async def get_update(self, uid):
        return await self._one(select(updates).where(updates.c.id == uid))

    async def finish_update(self, uid, status, reason=""):
        """终结一条台账；已是终态的行不再改写（避免迟到回执覆盖 done）。"""
        return await self._run(
            update(updates).where(updates.c.id == uid)
            .where(updates.c.status.in_(("pending", "queued")))
            .values(status=status, reason=str(reason or "")[:40],
                    finished_at=int(time.time())))

    async def list_updates(self, limit=50):
        rows = await self._all(select(updates).order_by(updates.c.created_at.desc())
                               .limit(limit))
        return rows

    async def updates_summary(self):
        """面板用：各状态计数（500 台升级后先回答「升了多少、失败多少」）。"""
        rows = await self._all(select(updates.c.status.label("status"),
                                      func.count().label("n"))
                               .group_by(updates.c.status))
        return {r["status"]: r["n"] for r in rows}

    async def in_flight_update(self, pod):
        """该主机是否有未终结的升级：D2.3 的 in_flight 去重靠它。

        没有台账就只能靠版本比较猜，必然重复下发与重复下载（8–12MB/台）。
        """
        return await self._one(
            select(updates.c.id, updates.c.to_version)
            .where(updates.c.pod == pod)
            .where(updates.c.status.in_(("pending", "queued")))
            .order_by(updates.c.created_at.desc()).limit(1))

    async def queued_updates(self, pod):
        """该主机所有 `queued` 行（D2.3 重连补投的输入）。"""
        return await self._all(
            select(updates.c.id, updates.c.to_version)
            .where(updates.c.pod == pod)
            .where(updates.c.status == "queued"))

    async def mark_update_pending(self, uid):
        """`queued` → `pending`：消息真正投出去了，计时窗口从此刻开始。

        **必须同时刷新 `created_at`**：它在本表里的语义是「当前状态开始计时的时刻」，
        而 `sweep_update_timeouts` 正是拿它算 pending 的 30min 窗口。若不刷新，一条
        「3 天前下发、当时离线」的 queued 行会在重连补投的瞬间就被判 timeout ——
        刚投出去就被记成失败。运维真正关心的「何时发起的升级」在 `audit` 里有记录。
        """
        return await self._run(
            update(updates).where(updates.c.id == uid)
            .where(updates.c.status == "queued")
            .values(status="pending", created_at=int(time.time())))

    async def finish_updates_reaching(self, pod, agent_ver):
        """状态帧佐证：主机已上报某版本 → 目标不高于它的在途台账收敛为 done。

        回执帧只是辅助信号（execv 前可能来不及发），**状态帧才是权威**——
        以主机实际上报的 agent_ver 作数。
        """
        rows = await self._all(
            select(updates.c.id, updates.c.to_version)
            .where(updates.c.pod == pod)
            .where(updates.c.status.in_(("pending", "queued"))))
        done = []
        for r in rows:
            if not r["to_version"] or not _version_lt(agent_ver or "", r["to_version"]):
                done.append(r["id"])
        for uid in done:
            await self.finish_update(uid, "done")
        return len(done)

    async def sweep_update_timeouts(self, now=None, pending_ttl=1800, queued_ttl=7 * 86400):
        """在途升级收敛为 timeout。

        两档阈值是刻意的：pending 是「已下发、在线」，30min 足够下载 + 重启；
        queued 是「下发时离线，消息还在 Broker 排着」，主机可能几小时后才上线，
        30min 一到就记 timeout 会把「正常排队」误报成失败。
        """
        now = int(now or time.time())
        n = await self._run(
            update(updates).where(updates.c.status == "pending")
            .where(updates.c.created_at < now - pending_ttl)
            .values(status="timeout", reason="no_receipt", finished_at=now))
        n += await self._run(
            update(updates).where(updates.c.status == "queued")
            .where(updates.c.created_at < now - queued_ttl)
            .values(status="timeout", reason="queue_expired", finished_at=now))
        return n
