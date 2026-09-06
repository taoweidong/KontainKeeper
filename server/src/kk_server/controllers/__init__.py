"""控制器路由注册：REST /api/*。Agent 通道走 MQTT Broker，不再有 WS 端点。"""
from . import agent_update, audit, auth, commands, containers, health, stats


def register(app):
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(containers.router)
    app.include_router(commands.router)
    app.include_router(audit.router)
    app.include_router(stats.router)
    app.include_router(agent_update.router)
