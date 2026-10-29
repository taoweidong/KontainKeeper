"""Store 的 audit 域方法（QR29-G 第二轮拆分）：经 Mixin 并入门面，调用点零改动。

方言收敛点（_upsert/_ensure_schema/_column_ddl）不在本文件——它们留在 store.py
基座（AGENTS.md 硬约束：三库方言差异全部收在基座两处，不许散回各域）。
本文件的改动必须是纯搬动：混进任何行为变更即视为违规（拆回）。
"""


import json
import time

from sqlalchemy import func, insert, or_, select

from .tables import audit

# 审计 detail 的入库封顶（字符，QR29-S13）：批量下发的审计会塞下完整 pods/ids/argv，
# 老库的 TEXT 列只有 64KB——超限在「命令已发布之后」炸出 1406，形成部分写入
# （前端看不到台账但命令已经出去了）。8KB 足够容纳任何有溯源价值的载荷。
_AUDIT_DETAIL_MAX = 8192


class _StoreAuditMixin:

    # ---- 审计 ----
    async def add_audit(self, actor, action, detail=None):
        text = json.dumps(detail, ensure_ascii=False) if detail else ""
        if len(text) > _AUDIT_DETAIL_MAX:
            # 截断而不是让 INSERT 在严格模式 1406 上炸掉：审计的价值在
            # 「发生过什么」，前 8KB 加标记足以定位（QR29-S13）
            text = text[:_AUDIT_DETAIL_MAX - 11] + "(truncated)"
        await self._run(insert(audit).values(
            actor=str(actor)[:64], action=str(action)[:40],
            detail=text, ts=int(time.time())))

    @staticmethod
    def _audit_filters(actor=None, action=None, keyword=None):
        """审计筛选条件的唯一构造点：列表、计数、导出三处共用同一套语义。

        与 _command_filters 同理：条件若留在控制器里先取后滤，命中行会被
        LIMIT 截掉，「导出=所见」就破功了（评审 P1）。
        """
        conds = []
        if actor:
            conds.append(audit.c.actor == actor)
        if action:
            conds.append(audit.c.action == action)
        if keyword:
            # autoescape 对齐 _host_filters / _command_filters（QR29-S18）
            kw = str(keyword)[:64]
            conds.append(or_(audit.c.detail.contains(kw, autoescape=True),
                             audit.c.actor.contains(kw, autoescape=True),
                             audit.c.action.contains(kw, autoescape=True)))
        return conds

    async def list_audit(self, limit=200, offset=None, actor=None, action=None,
                         keyword=None):
        stmt = select(audit)
        for cond in self._audit_filters(actor=actor, action=action, keyword=keyword):
            stmt = stmt.where(cond)
        stmt = stmt.order_by(audit.c.id.desc()).limit(limit)
        if offset:
            stmt = stmt.offset(int(offset))
        return await self._all(stmt)

    async def count_audit(self, actor=None, action=None, keyword=None):
        stmt = select(func.count().label("n")).select_from(audit)
        for cond in self._audit_filters(actor=actor, action=action, keyword=keyword):
            stmt = stmt.where(cond)
        row = await self._one(stmt)
        return row["n"] if row else 0
