"""Agent 自更新接口：

- POST /api/system/agent       管理员上传新版本二进制（multipart: file + version）
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
import shutil

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
        if os.path.exists(dest):  # 保留上一版，便于回滚
            try:
                shutil.move(dest, dest + ".prev")
            except OSError:
                pass
        with open(dest, "wb") as f:
            f.write(data)
        if os.name == "posix":
            os.chmod(dest, 0o755)

    await asyncio.to_thread(_write)

    sha = hashlib.sha256(data).hexdigest()
    info = {"version": version, "sha256": sha, "size": len(data)}
    await request.app.state.store.set_agent_latest(info)
    await request.app.state.store.add_audit(user, "agent_upload", info)
    return {"ok": True, **info}


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
        "url": "/api/system/agent/download",
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
