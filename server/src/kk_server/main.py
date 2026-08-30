"""服务端装配入口：组装 app、启动清理线程，`python -m kk_server` 运行。

MVC 分层：
- models/     数据持久化（SQLite store、版本工具）
- services/   业务服务（Agent 连接中枢 hub、命令黑名单 security）
- controllers/ HTTP/WS 控制器（REST /api/*、WebSocket /ws/agent）
- web/        视图：静态管理界面（无框架单页，随包打包）
"""
import logging
import os
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import PROTO_VER, __version__
from .config import load_settings
from .controllers import register
from .models.store import Store
from .services.hub import Hub

log = logging.getLogger("kk.server")


def create_app(env=None):
    settings = load_settings(env)

    store = Store(settings.db_path)
    if store.ensure_admin(settings.admin_user, settings.admin_pass):
        log.info("admin account %r ensured (created or password refreshed)", settings.admin_user)
    if settings.admin_pass == "admin":
        log.warning("using default admin password 'admin', set KK_ADMIN_PASS before production!")

    hub = Hub(store, settings.agent_tokens, PROTO_VER, settings.enforced_interval)

    app = FastAPI(title="KontainKeeper", version=__version__)
    app.state.store = store
    app.state.hub = hub
    app.state.cmd_blacklist = settings.cmd_blacklist
    app.state.agent_bin_dir = settings.agent_bin_dir

    register(app)

    web_dir = Path(settings.web_dir or (Path(__file__).resolve().parent / "web"))
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    else:
        log.warning("web dir not found at %s, UI disabled", web_dir)

    stop = threading.Event()

    def cleaner():
        while not stop.wait(300):
            try:
                store.cleanup()
            except Exception:
                log.exception("cleanup pass failed")

    threading.Thread(target=cleaner, daemon=True, name="kk-cleaner").start()
    app.state.shutdown = stop
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
