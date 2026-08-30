"""Agent 自更新模块 updater 的单元测试（纯标准库，无网络）。"""
import hashlib
import os

from kk_agent import config as kk_config
from kk_agent import updater as updater


def test_version_compare():
    assert updater.version_lt("0.1.0", "0.2.0")
    assert updater.version_lt("0.1.9", "0.2.0")
    assert updater.version_lt("0.1.0", "0.1.0.1")
    assert not updater.version_lt("0.2.0", "0.1.0")
    assert not updater.version_lt("0.1.0", "0.1.0")
    assert updater.version_lt("1", "1.0.1")


def test_verify_and_replace_ok(tmp_path):
    data = b"\x7fELF-fake-binary-content"
    target = tmp_path / "kk-agent"
    target.write_bytes(b"old")
    sha = hashlib.sha256(data).hexdigest()
    assert updater.verify_and_replace(data, sha, str(target))
    assert target.read_bytes() == data


def test_verify_and_replace_mismatch(tmp_path):
    data = b"payload"
    target = tmp_path / "kk-agent"
    target.write_bytes(b"old")
    try:
        updater.verify_and_replace(data, "deadbeef" * 8, str(target))
        assert False, "expected RuntimeError on sha mismatch"
    except RuntimeError:
        pass
    assert target.read_bytes() == b"old"  # 未替换


def test_verify_and_replace_missing_sha_rejected(tmp_path):
    """安全承诺：清单缺失 sha256 时必须拒绝替换，防止无校验写入任意二进制。"""
    data = b"payload"
    target = tmp_path / "kk-agent"
    target.write_bytes(b"old")
    try:
        updater.verify_and_replace(data, "", str(target))
        assert False, "expected RuntimeError on missing sha256"
    except RuntimeError:
        pass
    assert target.read_bytes() == b"old"  # 未替换
    # 不残留临时文件
    assert list(tmp_path.glob(".kk-agent.update.*")) == []


def test_fetch_latest_available(monkeypatch):
    manifest = '{"available": true, "version": "0.2.0", "sha256": "abc", "size": 10, "url": "/x"}'
    monkeypatch.setattr(updater, "_http_get", lambda *a, **k: manifest)
    cfg = {"server": "wss://h", "token": "t", "update_url": "", "update_insecure": False}
    info = updater.fetch_latest(cfg, None)
    assert info["version"] == "0.2.0"


def test_fetch_latest_unavailable(monkeypatch):
    monkeypatch.setattr(updater, "_http_get", lambda *a, **k: '{"available": false}')
    cfg = {"server": "wss://h", "token": "t", "update_url": "", "update_insecure": False}
    assert updater.fetch_latest(cfg, None) is None


def test_fetch_latest_error_returns_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(updater, "_http_get", boom)
    cfg = {"server": "wss://h", "token": "t", "update_url": "", "update_insecure": False}
    assert updater.fetch_latest(cfg, None) is None


def test_apply_manifest_updates_and_execv(tmp_path, monkeypatch):
    data = b"\x7fELF-updated-binary"
    target = tmp_path / "kk-agent-test"
    target.write_bytes(b"old")
    sha = hashlib.sha256(data).hexdigest()

    captured = {}
    monkeypatch.setattr(updater, "download_binary", lambda *a, **k: data)
    monkeypatch.setattr(os, "execv", lambda p, argv: captured.setdefault("execv", (p, argv)))

    cfg = {"server": "wss://h", "token": "t", "update_url": "http://h",
           "agent_bin": str(target), "update_insecure": False}
    manifest = {"version": "9.9.9", "sha256": sha, "size": len(data), "url": "/api/system/agent/download"}

    assert updater.apply_manifest(cfg, None, manifest) is True
    assert target.read_bytes() == data           # 文件已原子替换
    assert captured["execv"][0] == str(target)   # 已 execv 自重启


def test_apply_manifest_not_newer_skips(tmp_path, monkeypatch):
    target = tmp_path / "kk-agent-test"
    target.write_bytes(b"old")
    called = {"dl": False}
    monkeypatch.setattr(updater, "download_binary", lambda *a, **k: called.__setitem__("dl", True))
    cfg = {"server": "wss://h", "token": "t", "update_url": "http://h",
           "agent_bin": str(target), "update_insecure": False}
    manifest = {"version": "0.0.1", "sha256": "x", "size": 1, "url": "/x"}
    assert updater.apply_manifest(cfg, None, manifest) is False
    assert not called["dl"]
    assert target.read_bytes() == b"old"


def test_download_binary_real_http():
    """回归：_build_opener(False) 必须返回带 .open() 的 opener，真实 http 下载可用。

    早前实现在非 insecure 路径返回 urllib.request 模块本身（无 .open 方法），
    导致真实 agent 自更新在 http 服务端上崩溃（AttributeError）。本测试走真实网络。
    """
    import http.server
    import threading

    payload = b"\x7fELF-real-download-regression"
    captured = {}

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = "http://127.0.0.1:%d/bin" % port
        data = updater.download_binary(url, "tok", None)  # insecure 默认 False
        assert data == payload
    finally:
        srv.shutdown()
