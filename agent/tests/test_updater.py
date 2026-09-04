"""Agent 自更新模块 updater 的单元测试（纯标准库，无网络）。

安全主线：**在确认替换目标是独立二进制之前，绝不下载、绝不落盘**（P0-1 回归）。
"""
import hashlib
import os
import sys

from kk_agent import config as kk_config
from kk_agent import updater as updater


def test_version_compare():
    assert updater.version_lt("0.1.0", "0.2.0")
    assert updater.version_lt("0.1.9", "0.2.0")
    assert updater.version_lt("0.1.0", "0.1.0.1")
    assert not updater.version_lt("0.2.0", "0.1.0")
    assert not updater.version_lt("0.1.0", "0.1.0")
    assert updater.version_lt("1", "1.0.1")


# ---- sha256 / HMAC 校验（与落盘动作已解耦）----

def _cfg(**kw):
    base = {"update_hmac_key": "", "update_require_sig": False}
    base.update(kw)
    return base


def _hmac_hex(key, data):
    import hmac as _h
    return _h.new(key.encode(), data, hashlib.sha256).hexdigest()


def test_verify_signature_ok():
    data = b"binary"
    manifest = {"sha256": hashlib.sha256(data).hexdigest()}
    assert updater._verify_signature(data, manifest, _cfg(), None) is True


def test_verify_signature_missing_sha_rejected():
    """清单不带 sha256 必须拒绝——安全承诺，防无校验写入任意二进制。"""
    assert updater._verify_signature(b"x", {}, _cfg(), None) is False


def test_verify_signature_mismatch_rejected():
    manifest = {"sha256": "deadbeef" * 8}
    assert updater._verify_signature(b"x", manifest, _cfg(), None) is False


def test_verify_signature_hmac_required_and_matches():
    data = b"binary"
    key = "s3cret"
    manifest = {"sha256": hashlib.sha256(data).hexdigest(),
                "sig": _hmac_hex(key, data)}
    assert updater._verify_signature(data, manifest, _cfg(update_hmac_key=key), None) is True


def test_verify_signature_hmac_mismatch_rejected():
    data = b"binary"
    manifest = {"sha256": hashlib.sha256(data).hexdigest(), "sig": "00" * 32}
    assert updater._verify_signature(data, manifest,
                                     _cfg(update_hmac_key="s3cret"), None) is False


def test_verify_signature_require_sig_without_key_rejected():
    data = b"binary"
    manifest = {"sha256": hashlib.sha256(data).hexdigest()}
    assert updater._verify_signature(data, manifest,
                                     _cfg(update_require_sig=True), None) is False


# ---- 原子替换 ----

def test_verify_and_replace_writes_atomically(tmp_path):
    data = b"\x7fELF-fake-binary-content"
    target = tmp_path / "kk-agent"
    target.write_bytes(b"old")
    assert updater.verify_and_replace(data, str(target)) is True
    assert target.read_bytes() == data
    assert list(tmp_path.glob(".kk-agent.update.*")) == [], "临时文件必须清干净"


def test_verify_and_replace_failure_leaves_target_untouched(tmp_path):
    """目标目录不可写时：原文件保持不动，且不残留临时文件。"""
    target = tmp_path / "nowhere" / "kk-agent"
    before = list(tmp_path.iterdir())
    try:
        updater.verify_and_replace(b"data", str(target))
        assert False, "expected OSError for missing directory"
    except OSError:
        pass
    assert list(tmp_path.iterdir()) == before


# ---- 清单拉取 ----

def _manifest_str(version="9.9.9", sha="abc"):
    return '{"available": true, "version": "%s", "sha256": "%s", "size": 10, "url": "/x"}' % (
        version, sha)


def test_fetch_latest_available(monkeypatch):
    monkeypatch.setattr(updater, "_http_get", lambda *a, **k: _manifest_str())
    cfg = {"token": "t", "update_url": "http://api", "update_insecure": False}
    info = updater.fetch_latest(cfg, None)
    assert info["version"] == "9.9.9"


def test_fetch_latest_without_update_url_skips_http(monkeypatch):
    """改用 MQTT 后 broker 地址推不出 HTTP API 基址：未配 KK_UPDATE_URL 必须整个跳过。"""
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        return _manifest_str()
    monkeypatch.setattr(updater, "_http_get", spy)
    cfg = {"token": "t", "update_url": "", "update_insecure": False}
    assert updater.fetch_latest(cfg, None) is None
    assert called["n"] == 0, "绝不能拿 broker 地址去拼 HTTP"


def test_fetch_latest_unavailable(monkeypatch):
    monkeypatch.setattr(updater, "_http_get", lambda *a, **k: '{"available": false}')
    cfg = {"token": "t", "update_url": "http://api", "update_insecure": False}
    assert updater.fetch_latest(cfg, None) is None


def test_fetch_latest_error_returns_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(updater, "_http_get", boom)
    cfg = {"token": "t", "update_url": "http://api", "update_insecure": False}
    assert updater.fetch_latest(cfg, None) is None


# ---- 形态闸门：P0-1 的回归锁 ----

def test_source_mode_never_touches_interpreter(monkeypatch, tmp_path):
    """源码形态（非 frozen）且未设 KK_AGENT_BIN 时：不下载、不落盘、不碰解释器。

    旧实现会下载二进制覆盖 sys.executable，直接摧毁容器里的 Python。
    """
    called = {"dl": 0}
    monkeypatch.setattr(updater, "download_binary",
                        lambda *a, **k: called.__setitem__("dl", called["dl"] + 1) or b"x")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(os, "execv", lambda *a: called.__setitem__("execv", True))
    exe_before = open(sys.executable, "rb").read(16) if os.path.exists(sys.executable) else None

    cfg = {"token": "t", "update_url": "http://api", "agent_bin": "", "update_insecure": False}
    manifest = {"version": "9.9.9", "sha256": "x", "size": 1, "url": "/x"}
    assert updater.apply_manifest(cfg, None, manifest) is False
    assert called["dl"] == 0, "形态未确认前不得下载"
    assert "execv" not in called
    if exe_before is not None:
        assert open(sys.executable, "rb").read(16) == exe_before, "解释器必须原封不动"


def test_refuses_to_replace_interpreter_even_when_forced(monkeypatch, tmp_path):
    """KK_AGENT_BIN 被显式指向解释器/源码：仍然拒绝，且一个字节都不写。"""
    monkeypatch.setattr(updater, "download_binary", lambda *a, **k: b"injected")
    monkeypatch.setattr(os, "execv", lambda *a: None)
    for bad in (sys.executable, str(tmp_path / "kk_agent.py")):
        cfg = {"token": "t", "update_url": "http://api", "agent_bin": bad,
               "update_insecure": False}
        manifest = {"version": "9.9.9", "sha256": "x", "size": 1, "url": "/x"}
        assert updater.apply_manifest(cfg, None, manifest) is False


def test_refuses_missing_target_file(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "download_binary", lambda *a, **k: b"x")
    cfg = {"token": "t", "update_url": "http://api",
           "agent_bin": str(tmp_path / "not-built-yet"), "update_insecure": False}
    manifest = {"version": "9.9.9", "sha256": "x", "size": 1, "url": "/x"}
    assert updater.apply_manifest(cfg, None, manifest) is False


def test_apply_manifest_updates_and_execv(tmp_path, monkeypatch):
    data = b"\x7fELF-updated-binary"
    target = tmp_path / "kk-agent-test"
    target.write_bytes(b"old")
    sha = hashlib.sha256(data).hexdigest()

    captured = {}
    monkeypatch.setattr(updater, "download_binary", lambda *a, **k: data)
    monkeypatch.setattr(os, "execv", lambda p, argv: captured.setdefault("execv", (p, argv)))

    cfg = {"token": "t", "update_url": "http://api",
           "agent_bin": str(target), "update_insecure": False}
    manifest = {"version": "9.9.9", "sha256": sha, "size": len(data),
                "url": "/api/system/agent/download"}

    assert updater.apply_manifest(cfg, None, manifest) is True
    assert target.read_bytes() == data
    assert captured["execv"][0] == str(target)


def test_apply_manifest_bad_sha_does_not_replace(tmp_path, monkeypatch):
    """下载成功但校验不过：目标必须保持原样。"""
    target = tmp_path / "kk-agent-test"
    target.write_bytes(b"old")
    monkeypatch.setattr(updater, "download_binary", lambda *a, **k: b"tampered")
    monkeypatch.setattr(os, "execv", lambda *a: None)
    cfg = {"token": "t", "update_url": "http://api", "agent_bin": str(target),
           "update_insecure": False}
    manifest = {"version": "9.9.9", "sha256": "00" * 32, "size": 8, "url": "/x"}
    assert updater.apply_manifest(cfg, None, manifest) is False
    assert target.read_bytes() == b"old"


def test_apply_manifest_not_newer_skips(tmp_path, monkeypatch):
    target = tmp_path / "kk-agent-test"
    target.write_bytes(b"old")
    called = {"dl": False}
    monkeypatch.setattr(updater, "download_binary", lambda *a, **k: called.__setitem__("dl", True))
    cfg = {"token": "t", "update_url": "http://api", "agent_bin": str(target),
           "update_insecure": False}
    manifest = {"version": "0.0.1", "sha256": "x", "size": 1, "url": "/x"}
    assert updater.apply_manifest(cfg, None, manifest) is False
    assert not called["dl"]
    assert target.read_bytes() == b"old"


def test_check_update_disabled_by_config(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(updater, "fetch_latest", lambda *a, **k: called.__setitem__("n", 1))
    assert updater.check_update({"update_disabled": True}, None) is False
    assert called["n"] == 0


def test_agent_bin_default_is_empty_not_interpreter():
    """配置层就得堵住：缺省不能是 sys.executable。"""
    cfg = kk_config.load(env={"KK_SERVER": "mqtt://h"})
    assert cfg["agent_bin"] == ""


def test_download_binary_real_http():
    """回归：_build_opener(False) 必须返回带 .open() 的 opener，真实 http 下载可用。

    早前实现在非 insecure 路径返回 urllib.request 模块本身（无 .open 方法），
    导致真实 agent 自更新在 http 服务端上崩溃（AttributeError）。本测试走真实网络。
    """
    import http.server
    import threading

    payload = b"\x7fELF-real-download-regression"

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
        data = updater.download_binary("http://127.0.0.1:%d/bin" % port, "tok", None)
        assert data == payload
    finally:
        srv.shutdown()


def test_download_binary_enforces_size_cap():
    """超过上限立即中断，不能被恶意/异常大的响应撑爆内存。"""
    import http.server
    import threading

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(4096))
            self.end_headers()
            self.wfile.write(b"z" * 4096)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        try:
            updater.download_binary("http://127.0.0.1:%d/bin" % port, "tok", None,
                                    max_bytes=1024)
            assert False, "expected RuntimeError on oversized payload"
        except RuntimeError as e:
            assert "too large" in str(e)
    finally:
        srv.shutdown()
