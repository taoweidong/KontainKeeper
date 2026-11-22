"""控制器路由注册：REST /api/*。Agent 通道走 MQTT Broker（QR1125 Phase 0 起另有
HTTP ingest 上行 /api/v1/ingest/* 并存），不再有 WS 端点。"""
from . import (agent_update, audit, auth, commands, containers, exporting,
               health, ingest, ipwhitelist, rbac, stats)


def register(app):
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(containers.router)
    app.include_router(commands.router)
    app.include_router(exporting.router)
    app.include_router(audit.router)
    app.include_router(stats.router)
    app.include_router(rbac.router)
    app.include_router(ipwhitelist.router)
    app.include_router(agent_update.router)
    app.include_router(agent_update.agent_router)
    # QR1125 Phase 0：HTTP 上行 ingest（Agent 真实源 IP 鉴权），赶在静态 catch-all 前
    app.include_router(ingest.agent_router)
