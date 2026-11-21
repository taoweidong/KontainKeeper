"""Store 的 IP 白名单域方法（QR1124）：动态名单 CRUD + 拦截记录按 IP 聚合。

方言收敛点（_upsert/_delete_batched）留在 store.py 基座，本文件只组装语句。
白名单校验语义复用 config.parse_ip_whitelist（启动解析与运行时组网同一套）；
拦截记录按 IP 聚合 upsert（ip 主键 → 有界），写入节流在桥接侧（_on_ip_rejected
按 IP 60s 一次），不重开 QR1120-S1 的逐帧灌库口子。ip_rejected 审计并行保留。
"""
import time

from sqlalchemy import delete, func, insert, select, update

from .tables import ip_rejects, ip_rules
from ..config import parse_ip_whitelist
from ..logsetup import get_logger

log = get_logger("kk.store")

# 拦截记录保留期与行数上限（janitor 周期清理，非逐帧）：按 IP 聚合后行数 = distinct IP，
# 30 天保留 + 5 万行上限双保险，给「持续轮换 IP 灌拒绝帧」留有限空间。
REJECT_RETAIN_DAYS = 30
REJECT_MAX_ROWS = 50000


class _StoreIpWhitelistMixin:

    # ---- 白名单规则 CRUD ----
    async def list_ip_rules(self):
        return await self._all(select(ip_rules).order_by(ip_rules.c.id))

    async def add_ip_rule(self, cidr, remark=""):
        """新增条目：校验复用启动解析同一语义。非法格式抛 ValueError（控制器转 422），
        重复 cidr 抛 KeyError（控制器转 409）。返回归一化后的 cidr（单 IP→/32）。"""
        nets = parse_ip_whitelist(cidr)
        if not nets:
            raise ValueError("cidr 不能为空")
        norm = str(nets[0])
        dup = await self._one(select(ip_rules.c.id).where(ip_rules.c.cidr == norm))
        if dup:
            raise KeyError(norm)
        await self._run(insert(ip_rules).values(
            cidr=norm, remark=str(remark or "")[:200], enabled=1,
            created=int(time.time())))
        return norm

    async def update_ip_rule(self, rule_id, enabled=None, remark=None):
        vals = {}
        if enabled is not None:
            vals["enabled"] = 1 if enabled else 0
        if remark is not None:
            vals["remark"] = str(remark)[:200]
        if not vals:
            return 0
        return await self._run(
            update(ip_rules).where(ip_rules.c.id == rule_id).values(**vals))

    async def delete_ip_rule(self, rule_id):
        return await self._run(delete(ip_rules).where(ip_rules.c.id == rule_id))

    async def has_enabled_ip_rules(self):
        row = await self._one(
            select(func.count().label("n")).select_from(ip_rules)
            .where(ip_rules.c.enabled == 1))
        return bool(row and row["n"])

    async def enabled_ip_networks(self):
        """运行时名单：enabled 行的 cidr 组网。坏值跳过 + 日志（写入侧已校验，
        这里防御存量脏数据），不让一条坏条目拖垮整张名单。"""
        rows = await self._all(select(ip_rules.c.cidr).where(ip_rules.c.enabled == 1))
        nets = []
        for r in rows:
            try:
                nets.extend(parse_ip_whitelist(r["cidr"]))
            except ValueError:
                log.warning("白名单条目格式非法，跳过: %r", r["cidr"])
        return nets

    async def seed_ip_rules_from_env(self, networks):
        """env KK_AGENT_IPS 首次播种（kv 标记缺则补，仿 seed_rbac「改过不重置」）：
        管理员删空后重启不再回填——DB 为运行时真相源，env 只是启动种子。"""
        if await self.kv_get("ip_rules_seeded"):
            return 0
        if not networks:
            return 0
        now = int(time.time())
        n = 0
        for net in networks:
            cidr = str(net)
            dup = await self._one(select(ip_rules.c.id).where(ip_rules.c.cidr == cidr))
            if not dup:
                await self._run(insert(ip_rules).values(
                    cidr=cidr, remark="env 种子", enabled=1, created=now))
                n += 1
        await self.kv_set("ip_rules_seeded", "1")
        return n

    # ---- 拦截记录聚合 ----
    async def record_ip_reject(self, ip, pod, kind):
        """按 IP 聚合 upsert：hits 累加、last_seen/pod/kind 刷新、first_seen 保留。
        调用方（桥接 _on_ip_rejected）跑在单线程事件循环且按 IP 60s 节流，同 IP 无
        并发写，read-then-upsert 安全；first_seen 不在 update_cols → 冲突时保留原值。"""
        ip = str(ip or "")[:64]
        now = int(time.time())
        old = await self._one(select(ip_rejects).where(ip_rejects.c.ip == ip))
        hits = (old["hits"] if old else 0) + 1
        first = old["first_seen"] if old else now
        await self._upsert(
            ip_rejects,
            {"ip": ip, "hits": hits, "pod": str(pod or "")[:120],
             "kind": str(kind or "")[:12], "first_seen": first, "last_seen": now},
            ["ip"], ["hits", "pod", "kind", "last_seen"])

    @staticmethod
    def _reject_filters(ip=None):
        # 列表/计数/导出共用同一套筛选（导出=所见，仿 _audit_filters）
        conds = []
        if ip:
            conds.append(ip_rejects.c.ip.contains(str(ip)[:64], autoescape=True))
        return conds

    async def list_ip_rejects(self, limit=200, offset=None, ip=None):
        stmt = select(ip_rejects)
        for cond in self._reject_filters(ip):
            stmt = stmt.where(cond)
        stmt = stmt.order_by(ip_rejects.c.last_seen.desc()).limit(limit)
        if offset:
            stmt = stmt.offset(int(offset))
        return await self._all(stmt)

    async def count_ip_rejects(self, ip=None):
        stmt = select(func.count().label("n")).select_from(ip_rejects)
        for cond in self._reject_filters(ip):
            stmt = stmt.where(cond)
        row = await self._one(stmt)
        return row["n"] if row else 0

    async def cleanup_ip_rejects(self, now=None, retain_days=REJECT_RETAIN_DAYS,
                                 max_rows=REJECT_MAX_ROWS):
        """保留期批删 + 行数上限守卫（evict 最旧）。由 maintenance.cleanup 周期调用。"""
        now = int(now or time.time())
        deleted = await self._delete_batched(
            ip_rejects, ip_rejects.c.last_seen < now - retain_days * 86400,
            ip_rejects.c.ip)
        total = await self.count_ip_rejects()
        evicted = 0
        while total - evicted > max_rows:
            batch = await self._all(
                select(ip_rejects.c.ip).order_by(ip_rejects.c.last_seen).limit(5000))
            if not batch:
                break
            keys = [r["ip"] for r in batch]
            evicted += await self._run(delete(ip_rejects).where(ip_rejects.c.ip.in_(keys)))
        return {"ip_rejects_deleted": deleted, "ip_rejects_evicted": evicted}