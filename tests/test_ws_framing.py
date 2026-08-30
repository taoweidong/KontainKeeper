"""WebSocket 帧编解码单测（纯函数，跨平台）。"""
import os
import struct

import pytest

from kk_ws import (FrameParser, OP_CLOSE, OP_PING, OP_TEXT, WSError, WSClient,
                   encode_frame)


def parse_all(frames_bytes, chunk=7):
    p = FrameParser()
    data = b"".join(frames_bytes)
    for i in range(0, len(data), chunk):
        p.feed(data[i:i + chunk])
    out = []
    while True:
        f = p.pop()
        if f is None:
            break
        out.append(f)
    return out


@pytest.mark.parametrize("n", [0, 5, 125, 126, 65535, 65536, 200000])
def test_roundtrip_masked(n):
    payload = os.urandom(n)
    frame = encode_frame(OP_TEXT, payload, mask=True)
    frames = parse_all([frame])
    assert frames == [(OP_TEXT, payload)]


@pytest.mark.parametrize("n", [0, 125, 126, 70000])
def test_roundtrip_unmasked(n):
    payload = os.urandom(n)
    frame = encode_frame(OP_TEXT, payload, mask=False)
    assert parse_all([frame]) == [(OP_TEXT, payload)]


def test_fragmentation_and_control_interleave():
    part1, part2, part3 = b"hello ", b"wor", b"ld"
    frag1 = bytes([0x01]) + bytes([len(part1)]) + part1          # FIN=0, TEXT
    ping = bytes([0x80 | OP_PING, 3]) + b"abc"
    frag2 = bytes([0x00]) + bytes([len(part2)]) + part2          # FIN=0, CONT
    frag3 = bytes([0x80]) + bytes([len(part3)]) + part3          # FIN=1, CONT
    frames = parse_all([frag1, ping, frag2, frag3])
    assert frames[0] == (OP_PING, b"abc")
    assert frames[1] == (OP_TEXT, b"hello world")


def test_oversize_frame_rejected():
    p = FrameParser()
    header = bytes([0x82, 0x80 | 127]) + struct.pack(">Q", 20 * 1024 * 1024)
    with pytest.raises(WSError):
        p.feed(header)


def test_masked_input_unmasked_by_parser():
    payload = b"secret"
    frame = encode_frame(OP_TEXT, payload, mask=True)
    assert parse_all([frame]) == [(OP_TEXT, payload)]


def test_url_parsing():
    ws = WSClient("wss://kk.example.com:8443/ws/agent")
    assert ws.secure and ws.host == "kk.example.com" and ws.port == 8443 and ws.path == "/ws/agent"
    ws = WSClient("ws://10.0.0.1/agent")
    assert not ws.secure and ws.port == 80
    with pytest.raises(WSError):
        WSClient("http://bad")
