"""布局回归灌数脚本（R1–R9 口径的 500 台种子）：往分析库灌 500 台主机 / 命令 / 审计 / 台账。

只写 service/kk-analysis.db（*.db 已 gitignore），不碰任何真实数据。
可重复执行：主机已灌过则跳过，命令/审计/台账每次续灌。
"""
import asyncio
import random
import sys
import time

sys.path.insert(0, "server/src")

from kk_server.models.store import Store  # noqa: E402

random.seed(42)

GROUPS = ["华东机房", "华北机房", "华南机房", "测试集群", ""]
VIRTS = ["metal", "vm", "container"]
OSES = ["Ubuntu 22.04 LTS", "Debian 12", "Rocky Linux 9", "Alpine 3.19"]
IMAGES = [
    "vscode-server:1.96.2",
    "code-server:4.99.1",
    "theia-ide:1.62.0",
    "openvscode:1.109.4",
]


async def seed_hosts(store: Store, now: int) -> None:
    for i in range(500):
        pod = f"vscode-{i:04d}"
        await store.upsert_container(
            pod,
            random.choice(IMAGES),
            "1.6.0" if i % 7 == 0 else "1.7.0",
            random.choice([30, 60, 60, 120]),
        )
        online = i % 11 != 0  # ~9% 离线
        await store.set_online(
            pod,
            online=online,
            ts=now - random.randint(0, 120),
            image=random.choice(IMAGES),
            agent_ver="1.6.0" if i % 7 == 0 else "1.7.0",
            ip=f"10.20.{i // 256}.{i % 256}",
            env={
                "virt": random.choice(VIRTS),
                "os": random.choice(OSES),
                "kernel": "6.8.0-45-generic",
                "arch": "x86_64",
            },
            group=random.choice(GROUPS),
            proto_ver=4 if i % 13 != 0 else 3,
        )
        if online:
            disk_alert = i % 23 == 0
            await store.record_hb(
                pod,
                {
                    "ts": now - random.randint(0, 30),
                    "interval": 60,
                    "ip": f"10.20.{i // 256}.{i % 256}",
                    "metrics": {
                        "cpu": round(random.uniform(2, 96), 1),
                        "mem_mb": round(random.uniform(200, 15000), 1),
                        "mem_total_mb": 16384,
                        "disks": {
                            "/": {
                                "total_mb": 102400,
                                "used_mb": 96000 if disk_alert else 30000,
                                "pct": 93.8 if disk_alert else round(random.uniform(10, 80), 1),
                            },
                            "/data": {
                                "total_mb": 512000,
                                "used_mb": 120000,
                                "pct": 23.4,
                            },
                        },
                        "procs_top": [
                            {
                                "pid": 1000 + j,
                                "name": f"proc{j}",
                                "user": "root",
                                "cpu": round(random.uniform(0, 40), 1),
                                "mem_mb": round(random.uniform(10, 900), 1),
                            }
                            for j in range(8)
                        ],
                        "users": [
                            {
                                "name": "root",
                                "terminal": "pts/0",
                                "host": "10.0.0.9",
                                "started": now - 3600,
                            }
                        ],
                        "sys": {
                            "os": random.choice(OSES),
                            "kernel": "6.8.0-45-generic",
                            "uptime_sec": 864000,
                        },
                    },
                },
            )


async def main() -> None:
    store = Store("service/kk-analysis.db")
    await store.setup()
    await store.ensure_admin("admin", "admin123")

    now = int(time.time())
    existing = await store.count_containers()
    if existing:
        print(f"containers already seeded: {existing}, skip host seeding")
    else:
        await seed_hosts(store, now)
        print("hosts seeded: 500")

    pods = [r["pod"] for r in await store.list_containers("summary", limit=500)]

    # 命令：3 个批次，覆盖 done/failed/running/pending/timeout
    statuses = ["done", "done", "done", "failed", "running", "pending", "timeout"]
    cmd_ids: list[str] = []
    for b in range(3):
        sample = random.sample(pods, 60)
        ids, _batch = await store.create_commands_batch(
            sample, "shell", ["df", "-h"], 30, "admin", batch_id=f"b-seed{b:012d}"
        )
        cmd_ids.extend(ids)
    for cid in cmd_ids:
        st = random.choice(statuses)
        await store.exec_sql(
            "UPDATE kk_commands SET status=?, elapsed_ms=?, out_tail=? WHERE id=?",
            (
                st,
                random.randint(50, 9000),
                "Filesystem      Size  Used Avail Use% Mounted on\n"
                "/dev/sda1       100G   37G   64G  37% /",
                cid,
            ),
        )
    # 单机详情页的「最近命令」：给第一台主机补几条
    first = pods[0]
    ids, _ = await store.create_commands_batch(
        [first] * 5, "shell", ["systemctl", "status", "docker"], 30, "admin"
    )
    for cid in ids:
        await store.exec_sql(
            "UPDATE kk_commands SET status='done', elapsed_ms=?, out_tail=? WHERE id=?",
            (
                1234,
                "● docker.service - Docker Application Container Engine\n"
                "   Active: active (running)",
                cid,
            ),
        )

    # 审计 300 条
    for _i in range(300):
        await store.add_audit(
            "admin",
            random.choice(
                ["login_ok", "cmd_create", "cmd_blocked", "agent_upgrade", "login_fail"]
            ),
            {"pods": 3, "kind": "shell"},
        )

    # 升级台账 40 条
    for i in range(40):
        await store.create_update(
            f"u-seed-{i:04d}",
            random.choice(pods),
            "1.6.0",
            "1.7.0",
            random.choice(["done", "done", "queued", "failed"]),
        )

    print(f"seeded: hosts={len(pods)} cmds={len(cmd_ids) + 5}")
    await store.close()


asyncio.run(main())
