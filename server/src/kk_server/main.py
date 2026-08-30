"""服务端入口：组装 app、启动清理线程，`python -m kk_server` 运行。"""
import logging
import os
import threading
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from . import PROTO_VER, __version__, api
from .hub import Hub
from .store import Store

log = logging.getLogger("kk.server")

DEFAULT_BLACKLIST = "rm -rf /,mkfs,reboot,shutdown,dd if=/dev/zero,chmod -R 777 /"


def create_app(env=None):
    env = dict(os.environ if env is None else env)

    db_path = env.get("KK_DB_PATH", "kk-server.db")
    tokens = [t.strip() for t in env.get("KK_AGENT_TOKENS", "dev-token").split(",") if t.strip()]
    admin_user = env.get("KK_ADMIN_USER", "admin")
    admin_pass = env.get("KK_ADMIN_PASS", "admin")

    store = Store(db_path)
    if store.ensure_admin(admin_user, admin_pass):
        log.info("admin account %r created", admin_user)
    if admin_pass == "admin":
        log.warning("using default admin password, set KK_ADMIN_PASS in production!")

    blacklist = [p.strip().lower() for p in env.get("KK_CMD_BLACKLIST", DEFAULT_BLACKLIST).split(",") if p.strip()]
    enforced_raw = env.get("KK_ENFORCED_INTERVAL", "").strip()
    enforced = int(enforced_raw) if enforced_raw.isdigit() else None
    hub = Hub(store, tokens, PROTO_VER, enforced)

    app = FastAPI(title="KontainKeeper", version=__version__)
    app.state.store = store
    app.state.hub = hub
    app.state.cmd_blacklist = blacklist
    app.state.agent_bin_dir = env.get("KK_AGENT_BIN_DIR", "agent_assets")

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "agents_online": len(hub.conns)}

    @app.websocket("/ws/agent")
    async def agent_ws(ws: WebSocket):
        await hub.agent_endpoint(ws)

    api.register(app)

    web_dir = Path(env.get("KK_WEB_DIR") or (Path(__file__).resolve().parent / "web"))
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
