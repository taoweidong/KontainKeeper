"""最小 WebSocket 客户端（RFC 6455），纯标准库，无第三方依赖。

仅实现 Agent 需要的能力：出站连接握手、文本帧收发（增量解析）、
ping/pong 自动应答、close。目标是用最低的内存占用维持长连接。
"""
import base64
import hashlib
import os
import re
import select
import socket
import ssl
import struct
import time
from collections import deque

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA
MAX_FRAME = 16 * 1024 * 1024
_CHUNK = 65536


class WSError(Exception):
    pass


class WSClosed(WSError):
    pass


def unmask(data, key):
    return bytes(b ^ key[i & 3] for i, b in enumerate(data))


def encode_frame(opcode, payload, mask=True):
    """编码一个 FIN=1 帧；客户端发出的帧必须掩码。"""
    head = bytes([0x80 | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        return head + key + unmask(payload, key)
    return head + payload


class FrameParser:
    """增量帧解析器：feed 任意分片的字节流，pop 出完整 (opcode, payload)。"""

    def __init__(self):
        self.buf = bytearray()
        self._frames = deque()
        self._frag_op = None
        self._frag_parts = []

    def feed(self, data):
        self.buf += data
        self._parse()

    def pop(self):
        if self._frames:
            return self._frames.popleft()
        return None

    def _complete(self, opcode, payload):
        self._frames.append((opcode, payload))

    def _parse(self):
        buf = self.buf
        while True:
            if len(buf) < 2:
                return
            b0, b1 = buf[0], buf[1]
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            off = 2
            if length == 126:
                if len(buf) < 4:
                    return
                length = struct.unpack(">H", buf[2:4])[0]
                off = 4
            elif length == 127:
                if len(buf) < 10:
                    return
                length = struct.unpack(">Q", buf[2:10])[0]
                off = 10
            if length > MAX_FRAME:
                raise WSError("frame too large: %d" % length)
            mkey = b""
            if masked:
                if len(buf) < off + 4:
                    return
                mkey = bytes(buf[off:off + 4])
                off += 4
            if len(buf) < off + length:
                return
            payload = bytes(buf[off:off + length])
            del buf[:off + length]
            if masked:
                payload = unmask(payload, mkey)
            if opcode >= 0x8:  # 控制帧不可分片
                self._complete(opcode, payload)
                continue
            if opcode == OP_CONT:
                if self._frag_op is None:
                    raise WSError("unexpected continuation frame")
                self._frag_parts.append(payload)
                if fin:
                    self._complete(self._frag_op, b"".join(self._frag_parts))
                    self._frag_op, self._frag_parts = None, []
            else:
                if fin:
                    self._complete(opcode, payload)
                else:
                    self._frag_op, self._frag_parts = opcode, [payload]


class WSClient:
    """非阻塞 WebSocket 客户端。主循环用 select 等待可读，再 drain() 取消息。"""

    def __init__(self, url):
        m = re.match(r"^(wss|ws)://([^/: \]]+|\[[0-9a-fA-F:]+\])(?::(\d+))?(/.*)$", url.strip())
        if not m:
            raise WSError("bad server url: %r" % url)
        self.secure = m.group(1) == "wss"
        host = m.group(2).strip("[]")
        self.host = host
        self.port = int(m.group(3) or (443 if self.secure else 80))
        self.path = m.group(4)
        self.sock = None
        self.parser = FrameParser()

    # ---- 连接 ----
    def connect(self, timeout=10, headers=None):
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        sock = raw
        if self.secure:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=self.host)
        sock.settimeout(timeout)
        leftover = self._handshake(sock, timeout, headers or {})
        sock.setblocking(False)
        self.sock = sock
        self.parser = FrameParser()
        if leftover:
            self.parser.feed(leftover)

    def _handshake(self, sock, timeout, headers):
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            "GET %s HTTP/1.1" % self.path,
            "Host: %s:%d" % (self.host, self.port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        lines += ["%s: %s" % (k, v) for k, v in headers.items()]
        req = ("\r\n".join(lines) + "\r\n\r\n").encode()
        sock.sendall(req)
        resp = b""
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in resp:
            if time.monotonic() > deadline:
                raise WSError("handshake timeout")
            chunk = sock.recv(_CHUNK)
            if not chunk:
                raise WSError("closed during handshake")
            resp += chunk
            if len(resp) > 65536:
                raise WSError("handshake response too large")
        head, _, rest = resp.partition(b"\r\n\r\n")
        hlines = head.decode("latin1").split("\r\n")
        if "101" not in hlines[0]:
            raise WSError("handshake rejected: %s" % hlines[0])
        accept = ""
        for line in hlines[1:]:
            k, _, v = line.partition(":")
            if k.strip().lower() == "sec-websocket-accept":
                accept = v.strip()
        expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if accept != expect:
            raise WSError("bad Sec-WebSocket-Accept")
        return rest

    # ---- 发送 ----
    def send_text(self, text):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def _send_frame(self, opcode, payload):
        frame = encode_frame(opcode, payload, mask=True)
        view = memoryview(frame)
        deadline = time.monotonic() + 15
        while view:
            try:
                n = self.sock.send(view)
            except (BlockingIOError, InterruptedError, ssl.SSLWantWriteError):
                if time.monotonic() > deadline:
                    raise WSError("send timeout")
                select.select([], [self.sock], [], 1.0)
                continue
            except OSError:
                raise WSClosed("send failed")
            view = view[n:]

    # ---- 接收 ----
    def drain(self):
        """非阻塞读取所有可用字节，返回完整文本消息列表；自动应答 ping。"""
        while True:
            try:
                data = self.sock.recv(_CHUNK)
            except (BlockingIOError, InterruptedError, ssl.SSLWantReadError):
                break
            except OSError:
                raise WSClosed("recv failed")
            if not data:
                raise WSClosed("eof")
            self.parser.feed(data)
        msgs = []
        while True:
            frame = self.parser.pop()
            if frame is None:
                break
            opcode, payload = frame
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
            elif opcode == OP_PONG:
                continue
            elif opcode == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except Exception:
                    pass
                raise WSClosed("peer sent close")
            elif opcode == OP_TEXT:
                msgs.append(payload.decode("utf-8", "replace"))
        return msgs

    # ---- 关闭 ----
    def close(self):
        if self.sock is None:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack(">H", 1000))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None
