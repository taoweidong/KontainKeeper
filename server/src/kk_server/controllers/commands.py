"""命令下发与结果查询（带审计与黑名单）。

支持三类下发：
- kind=shell   执行 Linux 命令：argv 数组直传，或 cmdline 经 shlex 拆分
- kind=collect 按项采集指标：不经 shell，items 取自 COLLECT_ITEMS 白名单
- kind=plugin_reload 让 Agent 重扫采集插件目录
"""
import json
import shlex
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..config import COLLECT_ITEMS, COMMAND_KINDS
from ..services.security import is_blacklisted
from .deps import current_user

router = APIRouter(prefix="/api")

# collect 的 argv 列存这个结构；items 上限防误传
MAX_ITEMS = len(COLLECT_ITEMS)


class CommandBody(BaseModel):
    pods: List[str]
    kind: str = "shell"
    argv: Optional[List[str]] = None
    cmdline: Optional[str] = None
    items: Optional[List[str]] = None
    use_shell: bool = False
    timeout: int = 30


@router.get("/collect/items")
async def list_collect_items(request: Request):
    """前端「指标项 × 主机」勾选面板的数据源。"""
    await current_user(request)
    return {"items": COLLECT_ITEMS}


def _build_payload(body: CommandBody):
    """校验并归一化下发载荷，返回入库用 argv（shell 存数组，其余存结构体）。"""
    if body.kind not in COMMAND_KINDS:
        raise HTTPException(status_code=400,
                            detail="kind 需为 %s" % " / ".join(COMMAND_KINDS))

    if body.kind == "collect":
        items = [i.strip() for i in (body.items or []) if i and i.strip()]
        if not items:
            raise HTTPException(status_code=400, detail="collect 需要 items")
        unknown = [i for i in items if i not in COLLECT_ITEMS]
        if unknown:
            raise HTTPException(status_code=400,
                                detail="采集项不存在: %s（可用: %s）"
                                       % (", ".join(unknown), ", ".join(COLLECT_ITEMS)))
        return {"items": items}

    if body.kind == "plugin_reload":
        return {}

    # shell
    if body.argv:
        argv = list(body.argv)
    elif body.cmdline:
        if body.use_shell:
            # shell 模式原样交给 sh -c，不做 shlex 拆分（管道/重定向是有意保留的能力）
            argv = [body.cmdline]
        else:
            try:
                argv = shlex.split(body.cmdline)
            except ValueError as e:
                raise HTTPException(status_code=400, detail="命令行解析失败: %s" % e)
    else:
        raise HTTPException(status_code=400, detail="argv 或 cmdline 必填其一")
    if not argv:
        raise HTTPException(status_code=400, detail="命令不能为空")
    if body.use_shell:
        # 入库带标记，桥接据此还原 use_shell 字段；Agent 侧还有 KK_ALLOW_SHELL 二次开关
        return {"argv": argv, "use_shell": True}
    return argv


@router.post("/commands")
async def create_commands(body: CommandBody, request: Request):
    user = await current_user(request)
    store, bridge = request.app.state.store, request.app.state.bridge

    payload = _build_payload(body)
    if not body.pods:
        raise HTTPException(status_code=400, detail="目标容器不能为空")
    if bridge is None:
        raise HTTPException(status_code=503, detail="服务端未配置 KK_MQTT_URL，命令通道不可用")

    # 先一次查全量校验存在性，再批量建、逐条发布：
    # 避免「部分已下发、部分 404」造成状态撕裂，也避免 N 次单查（500 台一次点击）
    missing = sorted(set(body.pods) - await store.containers_exist(body.pods))
    if missing:
        raise HTTPException(status_code=404, detail="容器不存在: %s" % ", ".join(missing))

    timeout = min(max(body.timeout, 1), 600)
    # 黑名单只约束真正会执行命令的形态；collect/plugin_reload 不经 shell
    argv_for_check = payload.get("argv") if isinstance(payload, dict) else payload
    # use_shell 形态下 argv[0] 是整条命令串，必须按 shell 语义切分后再校验，
    # 否则 basename(argv[0]) 取不到程序名，结构校验整体失效（代码审查 P0-1）
    use_shell = isinstance(payload, dict) and bool(payload.get("use_shell"))
    if body.kind == "shell" and is_blacklisted(argv_for_check,
                                               request.app.state.cmd_blacklist,
                                               use_shell=use_shell):
        await store.add_audit(user, "command_blocked", {"argv": argv_for_check, "pods": body.pods})
        raise HTTPException(status_code=400, detail="命令命中黑名单，已被拒绝并记录审计")

    ids = await store.create_commands_batch(body.pods, body.kind, payload, timeout, user)
    created = []
    for cid, pod in zip(ids, body.pods):
        # dispatch_command 是 paho 的非阻塞发布（线程安全），不是数据库调用
        sent = bridge.dispatch_command(await store.get_command(cid))
        if sent:
            await store.mark_sent(cid)
        created.append({"id": cid, "pod": pod, "status": "sent" if sent else "pending"})
    await store.add_audit(user, "command_create",
                    {"kind": body.kind, "argv": payload, "pods": body.pods,
                     "ids": [c["id"] for c in created]})
    return {"items": created}


@router.get("/commands")
async def list_commands(request: Request, pod: Optional[str] = None, limit: int = 100):
    await current_user(request)
    return {"items": await request.app.state.store.list_commands(pod=pod, limit=min(limit, 500))}


@router.get("/commands/{cid}")
async def get_command(cid: str, request: Request):
    await current_user(request)
    row = await request.app.state.store.get_command(cid)
    if not row:
        raise HTTPException(status_code=404, detail="命令不存在")
    try:
        row["argv"] = json.loads(row["argv"])
    except ValueError:
        pass
    # 全量 out_b64 不进响应体：4MB 输出会撑出 5MB JSON。完整输出走 /out
    from ..models.store import _b64_tail
    row["out_tail"] = _b64_tail(row.pop("out_b64", "") or "")
    return row


@router.get("/commands/{cid}/out")
async def get_command_out(cid: str, request: Request, format: str = "text"):
    """完整输出单独走这个接口：列表只给 out_tail，避免大字段拖慢轮询。"""
    await current_user(request)
    if format not in ("text", "base64"):
        raise HTTPException(status_code=400, detail="format 需为 text 或 base64")
    out = await request.app.state.store.command_output(cid, as_text=(format == "text"))
    if out is None:
        raise HTTPException(status_code=404, detail="命令不存在")
    if format == "base64":
        return {"id": cid, "format": "base64", "out": out}
    return PlainTextResponse(out)
