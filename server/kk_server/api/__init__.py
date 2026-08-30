"""REST API 路由注册。"""
from . import agent_update, audit_routes, auth_routes, commands, containers, tokens


def register(app):
    app.include_router(auth_routes.router)
    app.include_router(containers.router)
    app.include_router(commands.router)
    app.include_router(tokens.router)
    app.include_router(audit_routes.router)
    app.include_router(agent_update.router)
