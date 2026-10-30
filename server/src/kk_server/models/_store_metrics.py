import time

"""Store 的 metrics 域方法（QR29-G 第二轮拆分）：经 Mixin 并入门面，调用点零改动。

方言收敛点（_upsert/_ensure_schema/_column_ddl）不在本文件——它们留在 store.py
基座（AGENTS.md 硬约束：三库方言差异全部收在基座两处，不许散回各域）。
本文件的改动必须是纯搬动：混进任何行为变更即视为违规（拆回）。
"""


from sqlalchemy import func, select

from .helpers import shards
from .tables import heartbeats, hourly, kv


class _StoreMetricsMixin:

    async def metrics_series(self, pod, hours=24):
        since = int(time.time()) - hours * 3600
        # 序列形状双路统一为 {ts, cpu, mem_mb, mem_pct, disk_pct}：
        # raw = 每帧原值（disk 为多挂载点最大值），hourly = 小时聚合均值。
        # NULL = 该桶/该帧没数据（老行、采集被关），前端画断线而不是 0。
        if hours <= 24:
            rows = await self._all(select(heartbeats.c.ts, heartbeats.c.cpu,
                                          heartbeats.c.mem_mb,
                                          heartbeats.c.mem_pct,
                                          heartbeats.c.disk_pct)
                                   .where(heartbeats.c.pod == pod)
                                   .where(heartbeats.c.ts >= since)
                                   .order_by(heartbeats.c.ts))
            return rows, "raw"
        rows = await self._all(select((hourly.c.hour * 3600).label("ts"),
                                      hourly.c.cpu_avg.label("cpu"),
                                      hourly.c.mem_avg.label("mem_mb"),
                                      hourly.c.mem_pct_avg.label("mem_pct"),
                                      hourly.c.disk_avg.label("disk_pct"))
                               .where(hourly.c.pod == pod)
                               .where(hourly.c.hour >= since // 3600)
                               .order_by(hourly.c.hour))
        return rows, "hourly"

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
                       func.max(heartbeats.c.mem_mb).label("mem_max"),
                       func.avg(heartbeats.c.mem_pct).label("mem_pct_avg"),
                       func.avg(heartbeats.c.disk_pct).label("disk_avg"))
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
                    "mem_pct_avg": r["mem_pct_avg"], "disk_avg": r["disk_avg"],
                    "last_metrics": ""},
                    ["pod", "hour"],
                    ["samples", "cpu_avg", "cpu_max", "mem_avg", "mem_max",
                     "mem_pct_avg", "disk_avg"])
            n += 1
        await self.kv_set("agg_hour", now_hour - 1)
        return n
