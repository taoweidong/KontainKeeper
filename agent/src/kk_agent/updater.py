"""Agent 自更新：下载 → 校验 → 原子替换 → 自重启。

【P0 修复记录】
旧实现的顺序是「先 verify_and_replace 落盘，再 _is_binary_target 判断形态」，
而 agent_bin 缺省值是 sys.executable。于是在源码形态下运行（含 README 推荐的
`uv run kk-agent`）时，下载的二进制会**直接覆盖 Python 解释器**，造成不可逆破坏。
当时所有测试都显式传了 agent_bin，该路径零覆盖。

新实现的铁律：**在确认「替换目标是独立二进制」之前，绝不下载、绝不落盘。**
判定顺序为：
  1. 版本是否更新（不更新直接返回）
  2. 替换目标是否已确定（未配置且非 frozen 形态 → 拒绝并提示）
  3. 目标是不是独立二进制（名字含 python/.py/.pyc → 拒绝）
  4. 以上全部通过，才下载 → 校验 → 原子替换 → execv

安全边界（保持纯标准库）：
- sha256 防传输损坏/截断
- 可选 HMAC-SHA256 签名（KK_UPDATE_HMAC_KEY）防伪造更新，无需引入第三方依赖；
  配置 KK_UPDATE_REQUIRE_SIG=1 时，缺少签名的清单一律拒绝
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse

from . import config as kk_config

UPDATE_PATH = "/api/system/agent"
MAX_BIN_BYTES = 64 * 1024 * 1024  # 单文件上限 64MB，防 OOM
_CHUNK = 256 * 1024

# 串行化自更新（轮询检查与服务端推送可能并发触发），避免两次下载竞争同一二进制
_update_lock = threading.Lock()


class _Null:
    """log=None 时的无操作占位，避免调用方判空。"""

    def __getattr__(self, _name):
        return lambda *a, **k: None

    def __bool__(self):
        return False


def _log(log):
    return log or _Null()


def parse_version(v):
    """'1.2.3' -> (1, 2, 3)；非数字段记 0。"""
    out = []
    for p in str(v or "").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def version_lt(a, b):
    """a < b ?"""
    pa, pb = parse_version(a), parse_version(b)
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    return pa < pb


def _api_base(cfg):
    """管理 API 基址只接受显式配置（KK_UPDATE_URL）。

    旧实现会从 WebSocket 地址推导，改用 MQTT 后 broker 地址与 HTTP API 地址不再同源，
    推导只会产生错误的 URL，因此这里要求显式配置；未配置则跳过本次检查。
    """
    return (cfg.get("update_url") or "").strip().rstrip("/")


def _build_opener(insecure):
    if not insecure:
        return urllib.request.build_opener()
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def _http_get(url, token, timeout=15, as_bytes=False, insecure=False):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    with _build_opener(insecure).open(req, timeout=timeout) as resp:
        data = resp.read()
    return data if as_bytes else data.decode("utf-8", "replace")


def fetch_latest(cfg, log):
    """拉取最新版本清单；未配置 API 基址 / 无更新 / 不可达时返回 None。"""
    log = _log(log)
    base = _api_base(cfg)
    if not base:
        return None
    url = "%s%s/latest?ver=%s" % (base, UPDATE_PATH, kk_config.AGENT_VER)
    try:
        info = json.loads(_http_get(url, cfg.get("token", ""), timeout=15,
                                    insecure=cfg.get("update_insecure")))
    except Exception as e:
        log.debug("fetch latest version failed: %s", e)
        return None
    return info if isinstance(info, dict) and info.get("available") else None


def _default_target():
    """仅打包后的独立二进制（PyInstaller）可自替换；源码运行一律不自替换。

    sys.frozen 是 PyInstaller/Nuitka 等打包器设置的标记，此时 sys.executable
    就是 Agent 自身的二进制路径，替换它是安全的。
    """
    if getattr(sys, "frozen", False):
        return sys.executable
    return ""


def _is_binary_target(target):
    """替换目标必须是独立二进制，绝不能是解释器或源码文件。"""
    name = os.path.basename(str(target)).lower()
    if name.endswith((".py", ".pyc", ".pyo", ".pyw")):
        return False
    if "python" in name:
        return False
    return True


def _verify_signature(data, manifest, cfg, log):
    """校验 sha256（防损坏）+ 可选 HMAC 签名（防伪造）。返回是否放行。"""
    log = _log(log)
    expected = str(manifest.get("sha256") or "")
    if not expected:
        log.warning("manifest missing sha256, refuse to replace")
        return False
    digest = hashlib.sha256(data).hexdigest()
    if digest.lower() != expected.lower():
        log.warning("sha256 mismatch: got %s expect %s", digest, expected)
        return False

    key = (cfg.get("update_hmac_key") or "").encode()
    sig = str(manifest.get("sig") or "")
    if key:
        mac = hmac.new(key, data, hashlib.sha256).hexdigest()
        if not sig or not hmac.compare_digest(mac, sig.lower()):
            log.warning("HMAC signature mismatch, refuse to replace")
            return False
    elif cfg.get("update_require_sig"):
        log.warning("KK_UPDATE_REQUIRE_SIG=1 but manifest carries no signature, refuse")
        return False
    return True


def download_binary(url, token, log, insecure=False, max_bytes=MAX_BIN_BYTES):
    """分块下载二进制，带大小上限保护。"""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    buf = bytearray()
    with _build_opener(insecure).open(req, timeout=60) as resp:
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise RuntimeError("binary too large: >%d bytes" % max_bytes)
    return bytes(buf)


def verify_and_replace(data, target):
    """写临时文件 → fsync → 原子替换 target。失败清理临时文件。"""
    d = os.path.dirname(os.path.abspath(target))
    tmp = os.path.join(d, ".kk-agent.update.%d" % os.getpid())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o755)
        os.replace(tmp, target)  # 同文件系统内原子替换
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return True


def apply_manifest(cfg, log, manifest):
    """按服务端清单下载、校验、替换并自重启。

    铁律：形态校验全部通过后才允许下载与落盘。任一步不满足即返回 False，
    且不产生任何副作用。
    """
    log = _log(log)
    ver = manifest.get("version")
    if not ver or not version_lt(kk_config.AGENT_VER, ver):
        return False

    target = cfg.get("agent_bin") or _default_target()
    if not target:
        log.info("agent %s available, but no self-replace target configured "
                 "(source-mode run); set KK_AGENT_BIN to enable self-update", ver)
        return False
    if not _is_binary_target(target):
        log.warning("refuse to self-update: target %r is not a standalone binary", target)
        return False
    if not os.path.exists(target):
        log.warning("refuse to self-update: target %r does not exist", target)
        return False

    base = _api_base(cfg)
    if not base:
        log.info("KK_UPDATE_URL not configured, skip update")
        return False
    url = manifest.get("url") or "%s/download" % UPDATE_PATH
    if not url.startswith("http"):
        url = base + (url if url.startswith("/") else "/" + url)

    log.info("agent update available: %s -> %s, downloading", kk_config.AGENT_VER, ver)
    with _update_lock:
        data = download_binary(url, cfg.get("token", ""), log, cfg.get("update_insecure"))
        if not _verify_signature(data, manifest, cfg, log):
            return False
        verify_and_replace(data, target)
        log.info("agent binary replaced (%d bytes); restarting", len(data))
        os.execv(target, [target] + sys.argv[1:])
    return True


def check_update(cfg, log):
    """轮询入口：拉清单 → 有更新则应用。设计为在一次性 daemon 线程内调用。"""
    log = _log(log)
    if cfg.get("update_disabled"):
        return False
    info = fetch_latest(cfg, log)
    if not info:
        return False
    try:
        return apply_manifest(cfg, log, info)
    except Exception:
        log.exception("agent self-update failed")
        return False


def spawn_check(cfg, log):
    threading.Thread(target=check_update, args=(cfg, log), daemon=True, name="kk-update").start()


def spawn_apply(cfg, log, manifest):
    threading.Thread(target=apply_manifest, args=(cfg, log, manifest), daemon=True,
                     name="kk-update-push").start()
