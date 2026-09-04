"""服务端装配入口：组装 app、拉起 MQTT 桥接与后台清理，`python -m kk_server` 运行。

MVC 分层：
- models/     数据持久化（SQLite store、版本工具）
- services/   业务服务（MQTT 桥接 bridge、命令黑名单 security）
- controllers/ HTTP 控制器（REST /api/*）
- web/        视图：静态管理界面（随包打包，由本服务托管）

Agent 侧的连接保活、鉴权、离线命令排队全部在 Broker（Mosquitto）完成，本进程不再
持有任何长连接，因此可多实例水平扩容——注意每实例的 KK_MQTT_CLIENT_ID 必须唯一。
"""
import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import PROTO_VER, __version__
from .config import load_settings
from .controllers import register
from .models.store import Store
from .services.mqtt_bridge import MqttBridge, SWEEP_INTERVAL

log = logging.getLogger("kk.server")
CLEANUP_PASSES = 10   # 每 N 个 sweep 周期做一次存储回收（30s × 10 = 5min）


def create_app(env=None):
    settings = load_settings(env)

    store = Store(settings.db_path)
    if store.ensure_admin(settings.admin_user, settings.admin_pass):
        log.info("admin account %r ensured (created or password refreshed)", settings.admin_user)
    if settings.admin_pass == "admin":
        log.warning("using default admin password 'admin', set KK_ADMIN_PASS before production!")

    # 未配 Broker 时允许只做只读管理（老库查看、审计导出），不拦启动
    bridge = (MqttBridge(store, settings, settings.agent_tokens, proto_ver=PROTO_VER)
              if settings.mqtt_url else None)
    if bridge is None:
        log.warning("KK_MQTT_URL 未配置：不连接 Broker，Agent 指标与命令通道不可用")

    stop = threading.Event()

    def janitor():
        n = 0
        while not stop.wait(SWEEP_INTERVAL):
            n += 1
            try:
                if bridge:
                    bridge.sweep()          # 命令超时收敛 + 僵尸在线判定
                if n % CLEANUP_PASSES == 0:
                    store.cleanup()
            except Exception:
                log.exception("janitor pass failed")

    @asynccontextmanager
    async def lifespan(app):
        # 桥接回调跑在 paho 网络线程，落库必须调度回这里拿到的事件循环
        if bridge is not None:
            bridge.loop = asyncio.get_running_loop()
            await asyncio.to_thread(bridge.start)
        worker = threading.Thread(target=janitor, daemon=True, name="kk-janitor")
        worker.start()
        try:
            yield
        finally:
            # 修 P2-13：原实现存了 Event 却从不 set，收尾逻辑形同虚设
            stop.set()
            worker.join(timeout=5)
            if bridge is not None:
                await asyncio.to_thread(bridge.stop)

    app = FastAPI(title="KontainKeeper", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.bridge = bridge
    app.state.agent_tokens = set(settings.agent_tokens)
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(
        create_app(),
        host=os.environ.get("KK_HOST", "0.0.0.0"),
        port=int(os.environ.get("KK_PORT", "8443")),
        log_level=os.environ.get("KK_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
