"""Agent 自更新接口：

- POST /api/system/agent       管理员上传新版本二进制（multipart: file + version）
- POST /api/system/agent/rollback 回滚待分发二进制到上一版（只影响重启的 Agent）
- GET  /api/system/agent/latest   Agent 查询最新版本清单（落后才 available）
- GET  /api/system/agent/download Agent 下载二进制（流式）

安全（v3）：
- 上传需管理员会话；下载/查询按请求方真实源 IP 校验 KK_AGENT_IPS 白名单
- 服务端记录 sha256，Agent 端下载后校验一致才替换，防损坏/篡改
- 二进制按平台单槽位（kk-agent），多架构需另行扩展
"""
import asyncio
import hashlib
import json
import os

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse

from .deps import agent_ip_auth, current_user
from ..models.version import version_lt

router = APIRouter(prefix="/api/system")

MAX_BIN_BYTES = 64 * 1024 * 1024
_BIN_NAME = "kk-agent"
_CHUNK = 256 * 1024


def _bin_path(request: Request):
    return os.path.join(request.app.state.agent_bin_dir, _BIN_NAME)


@router.post("/agent")
async def upload_agent(request: Request, file: UploadFile = File(...), version: str = Form(...)):
    user = await current_user(request)
    if not version or not version[0].isdigit():
        raise HTTPException(status_code=400, detail="version 非法")

    # 边读边累计，超限即断：避免先 `await file.read()` 把整包（最大 64MB）一次性读入内存
    data = bytearray()
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_BIN_BYTES:
            raise HTTPException(status_code=413, detail="二进制过大")
    if len(data) < 1:
        raise HTTPException(status_code=400, detail="二进制为空")

    bin_dir = request.app.state.agent_bin_dir
    dest = os.path.join(bin_dir, _BIN_NAME)

    def _write():
        """最大 64MB 的同步写不要占住事件循环——否则上传时所有心跳与请求都被卡住。"""
        os.makedirs(bin_dir, exist_ok=True)
        had_prev = False
        if os.path.exists(dest):  # 保留上一版，便于回滚
            try:
                # 必须用 os.replace（覆盖语义跨平台一致）：shutil.move 落到
                # os.rename，在 Windows 上遇到已存在的 .prev 会 FileExistsError，
                # 被下面的 except 吞掉 → 第二次上传起「上一版」静默不保留。
                os.replace(dest, dest + ".prev")
                had_prev = True
            except OSError:
                pass
        with open(dest, "wb") as f:
            f.write(data)
        if os.name == "posix":
            os.chmod(dest, 0o755)
        return had_prev

    # 上一版的**清单**要和二进制一起留下来（B2）：回滚时无从反推旧版本号，
    # 而 sha256 必须对得上 .prev 文件，否则 Agent 下载后会校验失败。
    store = request.app.state.store
    prev_info = await store.get_agent_latest()
    had_prev = await asyncio.to_thread(_write)

    sha = hashlib.sha256(data).hexdigest()
    info = {"version": version, "sha256": sha, "size": len(data)}
    await store.set_agent_latest(info)
    if had_prev and prev_info:
        await store.set_agent_prev(prev_info)
    elif not had_prev:
        await store.kv_set("agent_prev", "")   # 首次上传：清掉可能的历史残留
    await store.add_audit(user, "agent_upload", info)
    return {"ok": True, **info}


@router.post("/agent/rollback")
async def rollback_agent(request: Request):
    """把服务端**待分发**的 Agent 二进制回滚到上一版（B2 / P2-7）。

    语义边界（不说清就会被当成「一键回滚全网」）：
    - 只换服务端待分发的二进制与版本清单 —— **新上线或重启的 Agent** 才会拿到旧版；
    - 已在跑的 Agent **不会**因此降级：`version_lt` 只升不降，桥接的升级推送
      不会反向触发；真要让在跑的实例回退，只能让它们重启后重新走 latest 判定；
    - 本接口是「当前 ↔ 另一版」的**互换**：再调一次即撤销回滚（换回回滚前的
      那个版本）。`agent_prev` 恒记「另一版」的清单，文件路径则看它从哪来 ——
      上传留下的是 `.prev`，回滚留下的是 `.rollback`，两者都被认作候选。
    """
    user = await current_user(request)
    store = request.app.state.store
    dest = _bin_path(request)

    prev_info = await store.get_agent_prev()
    alt = dest + ".prev" if os.path.isfile(dest + ".prev") else dest + ".rollback"
    if not os.path.isfile(alt) or not prev_info:
        raise HTTPException(status_code=404, detail="没有可回滚的版本")
    cur_info = await store.get_agent_latest()

    def _swap():
        # 顺序要紧：当前版先暂存到 .swap，再让另一版上位，最后把暂存的当前版
        # 落为 .rollback（新备用）。若直接 replace(dest, ".rollback")，而备用
        # 恰好就叫 .rollback，会在搬走之前把它覆盖掉 —— 回滚一次后就没得撤了。
        # os.replace 的覆盖语义跨平台一致，Windows 上不会因目标已存在而失败。
        if os.path.exists(dest):
            tmp = dest + ".swap"
            os.replace(dest, tmp)
            os.replace(alt, dest)
            os.replace(tmp, dest + ".rollback")
        else:
            os.replace(alt, dest)
        if os.name == "posix":
            os.chmod(dest, 0o755)

    await asyncio.to_thread(_swap)
    await store.set_agent_latest(prev_info)
    if cur_info:
        await store.set_agent_prev(cur_info)
    await store.add_audit(user, "agent_rollback", {
        "from_version": (cur_info or {}).get("version", ""),
        "to_version": prev_info.get("version", ""),
    })
    return {"ok": True, **prev_info}


def _download_url(request: Request) -> str:
    """下发给 Agent 的下载地址：配了 KK_PUBLIC_URL 就给绝对地址（A6.1）。

    MQTT 化之后 Agent 无法从 broker 地址推导 HTTP API 地址，而镜像构建脚本不烧入
    KK_UPDATE_URL —— 相对地址会让推送式更新静默失效（Agent 侧只记一条 info 日志）。
    绝对地址让镜像侧零配置即可升级。
    """
    base = getattr(request.app.state.settings, "public_url", "") or ""
    path = "/api/system/agent/download"
    return base + path if base else path


@router.get("/updates")
async def list_updates(request: Request, limit: int = 50):
    """自更新台账（A6.2）：回答「这次上传的新版本，500 台升了多少、失败多少」。

    各状态计数 + 最近明细（含失败原因），逐台可核验。
    """
    await current_user(request)
    store = request.app.state.store
    limit = min(max(int(limit or 50), 1), 500)
    items, summary = await asyncio.gather(
        store.list_updates(limit=limit), store.updates_summary())
    return {"items": items, "summary": summary, "limit": limit}


@router.get("/agent/latest")
async def agent_latest(request: Request, ver: str = ""):
    await agent_ip_auth(request)
    latest = await request.app.state.store.get_agent_latest()
    if not latest:
        return JSONResponse({"available": False})
    if not version_lt(ver or "", latest.get("version", "")):
        return JSONResponse({"available": False})
    return {
        "available": True,
        "version": latest["version"],
        "sha256": latest.get("sha256", ""),
        "size": latest.get("size", 0),
        "url": _download_url(request),
    }


@router.get("/agent/download")
async def agent_download(request: Request):
    await agent_ip_auth(request)
    latest = await request.app.state.store.get_agent_latest()
    dest = _bin_path(request)
    if not latest or not os.path.isfile(dest):
        raise HTTPException(status_code=404, detail="no agent binary")

    def gen():
        with open(dest, "rb") as f:
            while True:
                b = f.read(_CHUNK)
                if not b:
                    break
                yield b

    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="%s"' % _BIN_NAME},
    )
