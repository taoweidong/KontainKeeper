"""Store 的命令离线队列域（QR1125 Phase 0）：经 Mixin 并入门面。

去 Broker 后命令离线排队不再靠 MQTT 持久会话，改由 DB queued 态承担——泛化
updates 域已验证的队列先例（_store_commands.py 的 queued_updates / mark_update_pending /
sweep_update_timeouts）：下发时主机离线 → 建 queued；SSE 重连 → flush 转 sent 补投；
超 TTL → timeout(queue_expired)。

方言收敛点不在本文件（留在 store.py 基座）；全部走 _all/_run + 既有 commands 表，
queued 是 status 列的新取值（String(12) 容得下，无需 _ADD_COLUMNS）。**idx_cmd_inflight
部分索引谓词（pending/sent/running）不变**——queued 由独立的 sweep_queued_command_timeouts
按 created_at + TTL 清扫，与在途命令的 timeout+slack 清扫互不干扰。
"""

import time

from sqlalchemy import select, update

from .helpers import shards
from .tables import commands


class _StoreCommandQueueMixin:

    async def queued_commands(self, pod):
        """该主机所有 queued 命令（SSE 重连补投的输入，仿 queued_updates）。

        按 created_at 升序——先排队的先补投，保持下发顺序。返回行含 dispatch_command
        重建帧所需的 id/pod/kind/argv/timeout（argv 是 JSON 串，dispatch 侧解析）。
        """
        return await self._all(
            select(commands.c.id, commands.c.pod, commands.c.kind,
                   commands.c.argv, commands.c.timeout, commands.c.created_at,
                   commands.c.batch_id)
            .where(commands.c.pod == pod)
            .where(commands.c.status == "queued")
            .order_by(commands.c.created_at))

    async def mark_command_queued(self, cids):
        """pending → queued：在线建行后发现主机离线时转排队（分片避变量上限）。"""
        cids = [c for c in (cids or []) if c]
        if not cids:
            return 0
        total = 0
        for shard in shards(cids):
            total += await self._run(
                update(commands)
                .where(commands.c.id.in_(shard))
                .where(commands.c.status == "pending")
                .values(status="queued"))
        return total

    async def mark_queued_commands_sent(self, cids):
        """queued → sent 且**刷 sent_at**：消息真正投出去了，计时窗口从此刻开始。

        与 mark_update_pending 刷 created_at 同型——sweep_command_timeouts 拿
        COALESCE(sent_at, created_at) + timeout 算窗口，若不刷新，一条「3 天前下发、
        当时离线」的 queued 行会在补投瞬间就被判 timeout（刚投出去就记失败）。
        只转 queued 行（终态/已在途的不拉回）。
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
                .where(commands.c.status == "queued")
                .values(status="sent", sent_at=now))
        return total

    async def sweep_queued_command_timeouts(self, now=None, ttl=86400):
        """queued 命令超 TTL 收敛为 timeout（仿 sweep_update_timeouts 的 queued 档）。

        独立于 sweep_command_timeouts（后者扫 pending/sent/running 按 timeout+slack）：
        queued 是「下发时离线、还在排队」，主机可能很久才上线，用命令自身的短 timeout
        判它会误报失败，故用独立的队列存活上限 ttl（KK_CMD_QUEUE_TTL，默认 24h）。
        commands 表无 reason 列，终态只盖 status + finished_at（与既有 timeout 同型）。
        """
        now = int(now or time.time())
        return await self._run(
            update(commands)
            .where(commands.c.status == "queued")
            .where(commands.c.created_at < now - int(ttl))
            .values(status="timeout", finished_at=now))