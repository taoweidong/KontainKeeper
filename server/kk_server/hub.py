"""Agent 连接中枢：注册、鉴权、心跳入库、命令路由与离线补发。"""
import asyncio
import json
import logging

from starlette.websockets import WebSocketDisconnect

log = logging.getLogger("kk.hub")


class Hub:
    def __init__(self, store, tokens, proto_ver=1, enforced_interval=None):
        self.store = store
        self.tokens = set(tokens)
        self.proto_ver = proto_ver
        self.enforced_interval = enforced_interval
        self.conns = {}  # pod -> WebSocket

    # ---- 在线查询 ----
    def is_online(self, pod):
        return pod in self.conns

    @property
    def online_pods(self):
        return list(self.conns.keys())

    # ---- Agent WebSocket 入口 ----
    async def agent_endpoint(self, ws):
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=15)
        except Exception:
            await ws.close(code=4400)
            return
        pod = str((hello or {}).get("pod") or "")[:120]
        token = hello.get("token") if isinstance(hello, dict) else None
        valid = (isinstance(hello, dict) and hello.get("t") == "hello"
                 and token in self.tokens and not self.store.is_token_revoked(token))
        if not valid:
            self.store.add_audit("agent", "hello_rejected", {"pod": pod})
            await ws.close(code=4401)
            return
        if int(hello.get("proto_ver") or 0) != self.proto_ver:
            await ws.close(code=4402)
            return

        interval = int(hello.get("interval") or 60)
        self.store.upsert_container(pod, hello.get("image", ""), hello.get("agent_ver", ""), interval)
        old = self.conns.get(pod)
        if old is not None and old is not ws:
            try:
                await old.close(code=4403)
            except Exception:
                pass
        self.conns[pod] = ws
        log.info("agent connected: %s (ver=%s interval=%ss)", pod, hello.get("agent_ver"), interval)

        if self.enforced_interval:
            try:
                await ws.send_json({"t": "cfg", "interval": self.enforced_interval})
            except Exception:
                pass

        # 离线期间积累的 pending 命令补发
        for row in self.store.pending_for(pod):
            if await self.try_dispatch(row):
                self.store.mark_sent(row["id"])

        try:
            while True:
                msg = await ws.receive_json()
                if not isinstance(msg, dict):
                    continue
                t = msg.get("t")
                if t == "hb":
                    self.store.record_hb(pod, msg)
                elif t == "cmd_result":
                    self.store.append_result(msg)
                elif t == "bye":
                    break
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as e:
            log.debug("agent %s connection error: %s", pod, e)
        finally:
            if self.conns.get(pod) is ws:
                del self.conns[pod]
                self.store.touch(pod)
            log.info("agent disconnected: %s", pod)

    # ---- 命令下发 ----
    async def try_dispatch(self, row):
        """向在线 Agent 发送命令；不在线/发送失败返回 False（保持 pending）。"""
        ws = self.conns.get(row["pod"])
        if ws is None:
            return False
        payload = {
            "t": "cmd",
            "id": row["id"],
            "kind": row["kind"],
            "argv": json.loads(row["argv"] or "[]"),
            "timeout": row["timeout"],
        }
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            if self.conns.get(row["pod"]) is ws:
                del self.conns[row["pod"]]
            return False
