"""Store 的 maintenance 域方法（QR29-G 第二轮拆分）：经 Mixin 并入门面，调用点零改动。

方言收敛点（_upsert/_ensure_schema/_column_ddl）不在本文件——它们留在 store.py
基座（AGENTS.md 硬约束：三库方言差异全部收在基座两处，不许散回各域）。
本文件的改动必须是纯搬动：混进任何行为变更即视为违规（拆回）。
"""


import json

from sqlalchemy import select

from .tables import kv


class _StoreMaintenanceMixin:

    async def kv_get(self, k, default=None):
        row = await self._one(select(kv.c.v).where(kv.c.k == k))
        return row["v"] if row else default

    async def kv_set(self, k, v):
        return await self._upsert(kv, {"k": k, "v": str(v)}, ["k"], ["v"])

    async def set_agent_latest(self, info):
        await self.kv_set("agent_latest", json.dumps(info, ensure_ascii=False))

    async def get_agent_latest(self):
        return await self._load_json_kv("agent_latest")

    async def set_agent_prev(self, info):
        await self.kv_set("agent_prev", json.dumps(info, ensure_ascii=False))

    async def get_agent_prev(self):
        return await self._load_json_kv("agent_prev")

    async def _load_json_kv(self, key):
        raw = await self.kv_get(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None
