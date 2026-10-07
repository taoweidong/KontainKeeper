"""真实数据库连接冒烟：验证 Store 在 PG / MySQL 上能建表、扩列、读写。

`test_dialects.py` 只做 SQL **静态编译**校验（`CreateTable().compile()`），编译得过
不等于运行时建得出表 —— MySQL 排序规则大小写、PG 标识符小写折叠这类问题只在
真连库时才暴露。本脚本补上「真连一次」这一环，被 Jenkins 的 dialects 阶段矩阵调用。

用法:
  KK_DB_URL="mysql+aiomysql://kk:kk@127.0.0.1:3306/kk" \
    python scripts/db_smoke.py
  KK_DB_URL="postgresql+asyncpg://kk:kk@127.0.0.1:5432/kk" \
    python scripts/db_smoke.py

隔离口径：直接用 `KK_DB_URL` 指向的库，跨 run 污染靠**uuid 后缀主机 id** 隔离，
结束时按同一 id 删掉本脚本写的行。此前这里是「随机库名」方案，但跑冒烟的账号
通常没有 `CREATE DATABASE` 权限（PG 报 `database ... does not exist`、MySQL 报
`Access denied for user ... to database`），于是 dialects 门禁其实从未通过过。

退出码非 0 即失败（CI 直接红）。
"""
import asyncio
import base64
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server", "src"))

from sqlalchemy import delete  # noqa: E402

from kk_server.models import tables  # noqa: E402
from kk_server.models.store import Store  # noqa: E402

# 单条命令输出的载荷尺寸：base64 后约 26 万字符，**必须越过 MySQL TEXT 的 64KB 上限**，
# 否则「大字段路径」只是注释里的承诺（此前 4KB 的载荷连 TEXT 都撑不满，等于没验）。
BIG_BYTES = 200_000


async def main():
    url = os.environ.get("KK_DB_URL")
    if not url:
        print("!! 必须设置 KK_DB_URL（如 mysql+aiomysql://kk:kk@127.0.0.1:3306/kk）")
        return 2

    host = "smoke-%s" % uuid.uuid4().hex[:8]
    store = Store(url)
    try:
        await store.setup()  # 建表 + _ensure_schema 扩列（跨方言最易炸的一步）

        # 走一遍真实读写链路，比单纯 setup 更贴近上线行为
        await store.upsert_container(host, "img:1", "0.3.0", 60)
        await store.set_online(host, True, image="img:1", agent_ver="0.3.0")
        await store.record_hb(host, {
            "interval": 60,
            "metrics": {"cpu": 12.0, "mem_mb": 800.0, "disks": {"/": {"pct": 90.0}}},
        })

        # 大字段分两帧发：一次覆盖 `_long_text()`（MySQL 必须 LONGTEXT），
        # 一次覆盖 `append_result` 在 SQL 里拼 base64 + seq 水位的真实链路。
        b64 = base64.b64encode(b"kk-smoke-output" * (BIG_BYTES // 15)).decode("ascii")
        half = len(b64) // 2 // 4 * 4          # 4 的倍数：两块各自也是合法 base64
        cid = await store.create_command(host, "shell", ["echo", "hi"], 30, "smoke")
        await store.append_result({"id": cid, "seq": 0, "total": 2,
                                   "out_b64": b64[:half]})
        await store.append_result({"id": cid, "seq": 1, "total": 2, "done": True,
                                   "rc": 0, "out_b64": b64[half:]})
        # QoS1「至少一次」：同 seq 重投必须被水位挡住，否则命令输出翻倍
        await store.append_result({"id": cid, "seq": 1, "total": 2, "done": True,
                                   "rc": 0, "out_b64": b64[half:]})

        row = await store.get_command(cid)
        assert row is not None and row["status"] == "done", "命令终态未落库"
        assert row["out_chunks"] == 2, "同 seq 重投未被幂等去重：out_chunks=%r" % (
            row["out_chunks"],)
        stored = await store.command_output(cid, as_text=False) or ""
        assert stored == b64, (
            "大字段回读不符：写入 %d 字符，读回 %d 字符"
            "（MySQL 列型不是 LONGTEXT 时会静默截断到 64KB）" % (len(b64), len(stored)))

        series, gran = await store.metrics_series(host, hours=1)
        assert gran == "raw" and len(series) >= 1, "心跳指标未落库"
        container = await store.get_container(host)
        assert container and container["online"], "set_online 未生效"

        print(">> dialects smoke ok: %s（主机 %s：%d 个指标点，命令 %s 输出 %d 字符已回读）"
              % (store.safe_url(), host, len(series), cid, len(b64)))
        return 0
    finally:
        # 普通 DELETE ... WHERE，按本脚本自己写的主机 id 精确回收，不含方言分支
        # （按主键/LIMIT 分批删才是三库分叉的坑，这里用不上）。
        try:
            async with store.engine.begin() as conn:
                for table in (tables.commands, tables.heartbeats,
                              tables.hourly, tables.containers):
                    await conn.execute(delete(table).where(table.c.pod == host))
        except Exception as exc:                     # 清理失败不能盖掉真正的红灯
            print("!! 清理 smoke 行失败（不影响结论）: %r" % (exc,))
        await store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
