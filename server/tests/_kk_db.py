"""测试库选择/复位助手（命名 _kk_db：agent/tests 与 server/tests 并存收集时，
conftest 这个模块名会被先入 sys.path 的那份抢走，唯一文件名避免串包）。

- KK_TEST_DB_URL 设了：全部测试统一打那个库（PG 验证用），pg_reset/kk_reset_sync
  做测试间隔离（TRUNCATE + 重放内置种子）；
- 没设：各用各的 SQLite 文件（tmp_path 天然隔离），复位函数立即返回。
"""
import asyncio
import os

from kk_server.models.store import Store


def kk_db_url(tmp_path) -> str:
    url = os.environ.get("KK_TEST_DB_URL", "").strip()
    return url or str(tmp_path / "test.db")


def kk_db_env(tmp_path) -> dict:
    """create_app / load_settings 的库环境：与 kk_db_url 同一口径。"""
    url = os.environ.get("KK_TEST_DB_URL", "").strip()
    return {"KK_DB_URL": url} if url else {"KK_DB_PATH": str(tmp_path / "test.db")}


async def _truncate_and_seed(store) -> None:
    async with store.engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE kk_audit, kk_commands, kk_containers, kk_heartbeats, "
            "kk_hourly, kk_updates, kk_sessions, kk_admins, kk_role_menus, "
            "kk_roles, kk_menus, kk_kv RESTART IDENTITY CASCADE")
    await store.seed_rbac()


async def pg_reset(store) -> None:
    """async 夹具用：TRUNCATE + 重放种子。SQLite 模式立即返回。"""
    if not os.environ.get("KK_TEST_DB_URL", "").strip():
        return
    await _truncate_and_seed(store)


def kk_reset_sync(url) -> None:
    """同步版复位：给「线程里跑 uvicorn」的集成夹具用。

    必须用**独立的 Store 与事件循环**——夹具 loop 里创建的 asyncpg 连接
    与 loop 绑定，被服务端线程的另一个 loop 复用就是
    "Future attached to a different loop"（2026-10-31 PG 首跑实测）。"""
    if not os.environ.get("KK_TEST_DB_URL", "").strip():
        return

    async def _run():
        st = Store(url)
        try:
            await st.setup()
            await _truncate_and_seed(st)
        finally:
            await st.close()

    asyncio.run(_run())
