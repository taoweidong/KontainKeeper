"""服务端装配入口：组装 app、拉起 MQTT 桥接与后台清理，`python -m kk_server` 运行。

MVC 分层：
- models/     数据持久化（SQLite store、版本工具）
- services/   业务服务（MQTT 桥接 bridge、命令黑名单 security）
- controllers/ HTTP 控制器（REST /api/*）
- web/        视图：静态管理界面（随包打包，由本服务托管）

MQTT 通道下 Agent 侧的连接保活、鉴权、离线命令排队在 Broker（Mosquitto）完成，
上行无状态可多实例水平扩容——注意每实例的 KK_MQTT_CLIENT_ID 必须唯一。
QR1125 Phase 0 起另有 HTTP ingest 上行 + SSE 下行命令通道并存（docs/design-transport.md）：
SSE 连接表是单进程内存态，多实例下行靠 DB queued + 重连 flush 保正确性（实时跨实例推留后续）。
"""
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import PROTO_VER, __version__
from .config import ensure_whitelist_configured, load_settings
from .controllers import register
from .controllers.deps import refresh_agent_ips
from .logsetup import get_logger, setup_logging
from .models.store import Store, normalize_url
from .services.mqtt_bridge import MqttBridge, SWEEP_INTERVAL
from .services.sse_hub import SseHub

log = get_logger("kk.server")
CLEANUP_PASSES = 10   # 每 N 个 sweep 周期做一次存储回收（30s × 10 = 5min）


def create_app(env=None):
    settings = load_settings(env)
    # 日志在装配期就配好（幂等）：测试也用 create_app 构造，日志行为需与生产一致
    setup_logging(settings)
    # 建库放 lifespan：Store 全异步，装配函数保持同步给 uvicorn/测试用
    store = Store(normalize_url(settings.db_url, settings.db_path))

    # 未配 Broker 时允许只做只读管理（老库查看、数据导出），不拦启动
    bridge = (MqttBridge(store, settings, settings.agent_ips, proto_ver=PROTO_VER,
                         accept_proto_vers=settings.accept_proto_vers)
              if settings.mqtt_url else None)
    if bridge is None:
        log.warning("KK_MQTT_URL 未配置：不连接 Broker，Agent 指标与命令通道不可用")
    elif not settings.agent_ips:
        log.warning("KK_AGENT_IPS 未配置：Agent 接入白名单为空，允许所有上报"
                    "（仅适用于开发/测试；生产请配置 KK_AGENT_IPS）")
    if settings.update_mode != "auto":
        # 默认值从「自动全网推」改成 manual 是有意的行为变更（D2.1）：必须显式说出来，
        # 否则存量部署会以为自更新坏了。需要旧行为就设 KK_UPDATE_MODE=auto。
        log.warning("KK_UPDATE_MODE=%s：自动升级已关闭，Agent 轮询也会被答「无需升级」；"
                    "请在「版本与更新」页手动选机升级（置 auto 恢复自动全网推）",
                    settings.update_mode)

    @asynccontextmanager
    async def lifespan(app):
        await store.setup()
        if await store.ensure_admin(settings.admin_user, settings.admin_pass):
            log.info("admin account %r ensured (created or password refreshed)",
                     settings.admin_user)
        if settings.admin_pass == "admin123":
            log.warning("using default admin password 'admin123', set KK_ADMIN_PASS "
                        "before production!")
        # QR1124：env 白名单首次播种（缺则补）→ 生产校验（env 或 DB 任一非空）→
        # 初始快照刷新。校验在建库后，故能认 DB 里的界面配置，不再只认 env。
        await store.seed_ip_rules_from_env(settings.agent_ips)
        await ensure_whitelist_configured(settings, store, env)
        await refresh_agent_ips(app, store, bridge)
        if bridge is not None:
            # 桥接回调跑在 paho 网络线程，落库要调度回这里拿到的循环
            bridge.loop = asyncio.get_running_loop()
            await asyncio.to_thread(bridge.start)
        janitor = asyncio.create_task(_janitor(app, store, bridge), name="kk-janitor")
        try:
            yield
        finally:
            # 修 P2-13：原实现存了个 Event 却从不 set，收尾形同虚设
            janitor.cancel()
            try:
                await janitor
            except asyncio.CancelledError:
                pass
            if bridge is not None:
                await asyncio.to_thread(bridge.stop)
            await store.close()

    async def _janitor(app, store, bridge):
        """周期任务：命令超时收敛 + 僵尸在线判定 + 存储回收 + 白名单快照兜底刷新。"""
        n = 0
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            n += 1
            try:
                if bridge is not None:
                    await bridge.sweep()
                # QR1125：回收停止续活的 SSE 死连接（生成器异常退出的兜底）
                dead = app.state.sse_hub.sweep_dead()
                if dead:
                    log.info("sse: 回收 %d 条死连接", dead)
                # QR1124：多实例兜底——别的实例改了 DB 名单，本实例 30s 内收敛
                await refresh_agent_ips(app, store, bridge)
                if n % CLEANUP_PASSES == 0:
                    stats = await store.cleanup()
                    log.info("cleanup: %s", stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("janitor pass failed")

    app = FastAPI(title="KontainKeeper", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.bridge = bridge
    # QR1125 Phase 0：SSE 下行连接表（空表装配，连接由 /api/v1/commands/stream 注册）
    app.state.sse_hub = SseHub()
    # 初始为 env 快照；lifespan 建库后 refresh_agent_ips 刷成 DB 运行时名单（QR1124）
    app.state.agent_ips = settings.agent_ips
    app.state.cmd_blacklist = settings.cmd_blacklist
    app.state.agent_bin_dir = settings.agent_bin_dir

    register(app)

    web_dir = Path(settings.web_dir or (Path(__file__).resolve().parent / "web"))
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    else:
        log.warning("web dir not found at %s, UI disabled", web_dir)
    return app


def main():
    import uvicorn
    # log_config=None：别让 uvicorn 自带的 dictConfig 覆盖我们的 sink/格式（A7.3）。
    # 其记录由 logsetup 的 InterceptHandler 统一收取，级别也由 KK_LOG_LEVEL 掌控。
    uvicorn.run(
        create_app(),
        host=os.environ.get("KK_HOST", "0.0.0.0"),
        port=int(os.environ.get("KK_PORT", "8443")),
        log_config=None,
        log_level=os.environ.get("KK_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
