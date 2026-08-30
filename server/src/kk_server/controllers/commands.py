"""命令下发与结果查询（带审计与黑名单）。"""
import json
import shlex
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..services.security import is_blacklisted
from .deps import current_user

router = APIRouter(prefix="/api")


class CommandBody(BaseModel):
    pods: List[str]
    kind: str = "shell"
    argv: Optional[List[str]] = None
    cmdline: Optional[str] = None
    timeout: int = 30


@router.post("/commands")
async def create_commands(body: CommandBody, request: Request):
    user = current_user(request)
    store, hub = request.app.state.store, request.app.state.hub

    if body.kind == "shell":
        if body.argv:
            argv = list(body.argv)
        elif body.cmdline:
            try:
                argv = shlex.split(body.cmdline)
            except ValueError as e:
                raise HTTPException(status_code=400, detail="命令行解析失败: %s" % e)
        else:
            raise HTTPException(status_code=400, detail="argv 或 cmdline 必填其一")
        if not argv:
            raise HTTPException(status_code=400, detail="命令不能为空")
    else:
        argv = []
    if not body.pods:
        raise HTTPException(status_code=400, detail="目标容器不能为空")

    # 先校验全部目标容器存在，再批量下发，避免「部分已下发、部分 404」造成状态撕裂
    missing = [p for p in body.pods if not store.get_container(p)]
    if missing:
        raise HTTPException(status_code=404, detail="容器不存在: %s" % ", ".join(missing))

    timeout = min(max(body.timeout, 1), 600)
    if is_blacklisted(argv, request.app.state.cmd_blacklist):
        store.add_audit(user, "command_blocked", {"argv": argv, "pods": body.pods})
        raise HTTPException(status_code=400, detail="命令命中黑名单，已被拒绝并记录审计")

    created = []
    for pod in body.pods:
        cid = store.create_command(pod, body.kind, argv, timeout, user)
        row = store.get_command(cid)
        sent = await hub.try_dispatch(row)
        if sent:
            store.mark_sent(cid)
        created.append({"id": cid, "pod": pod, "status": "sent" if sent else "pending"})
    store.add_audit(user, "command_create", {"argv": argv, "pods": body.pods, "kind": body.kind,
                                             "ids": [c["id"] for c in created]})
    return {"items": created}


@router.get("/commands")
def list_commands(request: Request, pod: Optional[str] = None, limit: int = 100):
    current_user(request)
    return {"items": request.app.state.store.list_commands(pod=pod, limit=min(limit, 500))}


@router.get("/commands/{cid}")
def get_command(cid: str, request: Request):
    current_user(request)
    row = request.app.state.store.get_command(cid)
    if not row:
        raise HTTPException(status_code=404, detail="命令不存在")
    try:
        row["argv"] = json.loads(row["argv"])
    except ValueError:
        pass
    return row
