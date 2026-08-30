"""控制器路由注册：REST(/api/*) + Agent WebSocket(/ws/agent)，对外接口契约不变。"""
from . import agent_update, agent_ws, audit, auth, commands, containers, health, tokens


def register(app):
    app.include_router(health.router)
    app.include_router(agent_ws.router)
    app.include_router(auth.router)
    app.include_router(containers.router)
    app.include_router(commands.router)
    app.include_router(tokens.router)
    app.include_router(audit.router)
    app.include_router(agent_update.router)
