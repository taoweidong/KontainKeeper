"""命令下发与结果查询（带审计与黑名单）。"""
import json
import os
import re
import shlex
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .deps import current_user

router = APIRouter(prefix="/api")


class CommandBody(BaseModel):
    pods: List[str]
    kind: str = "shell"
    argv: Optional[List[str]] = None
    cmdline: Optional[str] = None
    timeout: int = 30


def _blacklisted(argv, patterns):
    """命令黑名单：重点防护破坏性指令。

    比单纯子串匹配更抗绕过：
    1) 程序名 + 高危参数组合：rm/mv 带递归或强制；chmod/chown -R；dd；
       mkfs/reboot/shutdown/poweroff/halt/... 等直接拒绝。
    2) 管理员可配置的 KK_CMD_BLACKLIST 子串（折叠多余空白后匹配），兼容旧用法，
       并修复 "rm  -rf /" 这类双空格绕过。
    """
    if not argv:
        return False
    prog = os.path.basename(str(argv[0]).strip().lower())
    args = [str(a).lower() for a in argv[1:]]
    dangerous_combos = {
        "rm": {"-r", "-rf", "-fr", "-R", "--recursive", "-f", "--force"},
        "mv": {"-r", "-rf", "-fr", "-R", "--recursive", "-f", "--force"},
        "chmod": {"-R", "--recursive"},
        "chown": {"-R", "--recursive"},
    }
    if prog in dangerous_combos and (set(args) & dangerous_combos[prog]):
        return True
    if prog in {"dd", "mkfs", "reboot", "shutdown", "poweroff", "halt",
                "init", "fdisk", "parted", "wipefs", "lvremove", "pvremove", "vgremove"}:
        return True
    # 配置型子串黑名单（折叠多余空白，避免双空格绕过）
    norm = re.sub(r"\s+", " ", " ".join(str(a) for a in argv).lower())
    for p in patterns:
        if re.sub(r"\s+", " ", p.lower()) in norm:
            return True
    return False


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
    if _blacklisted(argv, request.app.state.cmd_blacklist):
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
