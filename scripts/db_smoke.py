"""真实数据库连接冒烟：验证 Store 在 PG / MySQL 上能建表、扩列、读写。

`test_dialects.py` 只做 SQL **静态编译**校验（`CreateTable().compile()`），编译得过
不等于运行时建得出表 —— MySQL 排序规则大小写、PG 标识符小写折叠这类问题只在
真连库时才暴露。本脚本补上「真连一次」这一环，被 Jenkins 的 dialects 阶段矩阵调用。

用法:
  KK_DB_URL="mysql+aiomysql://kk:kk@127.0.0.1:3306/kk" \
    python scripts/db_smoke.py
  KK_DB_URL="postgresql+asyncpg://kk:kk@127.0.0.1:5432/kk" \
    python scripts/db_smoke.py

退出码非 0 即失败（CI 直接红）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server", "src"))

from kk_server.models.store import Store


async def main():
    url = os.environ.get("KK_DB_URL")
    if not url:
        print("!! 必须设置 KK_DB_URL（如 mysql+aiomysql://kk:kk@127.0.0.1:3306/kk）")
        return 2

    # 共享 schema 会带来跨 run 污染，这里用随机库名隔离（CI 容器每次全新）
    import uuid
    db = "kk_smoke_%s" % uuid.uuid4().hex[:8]
    base = url.rsplit("/", 1)[0]
    target = "%s/%s" % (base, db)

    store = Store(target)
    try:
        await store.setup()  # 建表 + _ensure_schema 扩列（跨方言最易炸的一步）

        # 走一遍真实读写链路，比单纯 setup 更贴近上线行为
        await store.upsert_container("smoke-host", "img:1", "0.3.0", 60)
        await store.set_online("smoke-host", True, image="img:1", agent_ver="0.3.0")
        await store.record_hb("smoke-host", {
            "interval": 60,
            "metrics": {"cpu": 12.0, "mem_mb": 800.0, "disks": {"/": {"pct": 90.0}}},
        })
        # 大字段路径：MySQL 上必须是 LONGTEXT，否则这里会静默截断或报错
        cid = await store.create_command("smoke-host", "shell", ["echo", "hi"], 30, "smoke")
        await store.append_result({"id": cid, "seq": 0, "total": 1, "done": True,
                                   "rc": 0, "out_b64": "aGk=" * 1000})

        rows = await store.list_containers()
        assert any(r["pod"] == "smoke-host" for r in rows), "upsert_container 未落库"
        print(">> dialects smoke ok: %s (%d 主机落库, 命令 %s 已回写)"
              % (target, len(rows), cid))

        # 临时库由 CI 容器本身的生命周期回收（每次全新容器），这里不手动 DROP
        return 0
    finally:
        await store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
