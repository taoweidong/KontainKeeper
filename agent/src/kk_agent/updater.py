"""Agent 自更新：纯标准库实现，无需第三方依赖。

流程（由主循环或服务端 upgrade 帧触发）：
1. 拉取服务端最新版本清单  GET {base}/api/system/agent/latest?ver=<当前版本>
2. 若服务端版本更新，下载二进制 GET {base}/api/system/agent/download
3. 校验 sha256（服务端清单内附带），防止下载到损坏/被篡改的程序
4. 原子替换当前二进制文件（os.replace），再 os.execv 自重启

安全边界：
- 仅使用服务端下发的 sha256 做完整性校验（保持二进制零依赖）
- 默认走 https 证书校验；仅 KK_UPDATE_INSECURE=1 时关闭（不推荐）
- 二进制仅在「编译后的独立二进制」形态下自替换；源码运行（python -m kk_agent）
  只下载校验、不 execv，避免误改解释器
"""
import hashlib
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

# 串行化自更新（轮询检查与 push 升级帧可能并发触发），避免两个下载/替换竞争同一二进制
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


def _http_base(server, override=""):
    """由 WebSocket 地址推导管理 API 的 http(s) 基址。"""
    if override:
        return override.rstrip("/")
    s = server or ""
    if s.startswith("wss://"):
        s = "https://" + s[6:]
    elif s.startswith("ws://"):
        s = "http://" + s[5:]
    p = urlparse(s)
    base = (p.scheme or "http") + "://" + (p.netloc or p.path)
    return base.rstrip("/")


def _build_opener(insecure):
    """返回带 .open() 的 OpenerDirector。

    非 insecure：默认 opener（走系统 CA 校验证书）。
    insecure：关闭主机名/证书校验（仅 KK_UPDATE_INSECURE=1 时使用，不推荐）。
    """
    if not insecure:
        return urllib.request.build_opener()  # 默认 opener，支持 .open()
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def _http_get(url, token, timeout=15, as_bytes=False, insecure=False):
    """带 Bearer 鉴权的 GET；返回 bytes 或 str。4xx/5xx 抛 urllib.error.HTTPError。"""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    opener = _build_opener(insecure)
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read()
    return data if as_bytes else data.decode("utf-8", "replace")


def fetch_latest(cfg, log):
    """拉取最新版本清单；无更新/不可达返回 None。"""
    log = _log(log)
    base = _http_base(cfg.get("server", ""), cfg.get("update_url", ""))
    url = "%s%s/latest?ver=%s" % (base, UPDATE_PATH, kk_config.AGENT_VER)
    try:
        raw = _http_get(url, cfg.get("token", ""), timeout=15, insecure=cfg.get("update_insecure"))
        import json
        info = json.loads(raw)
    except Exception as e:
        log.debug("fetch latest version failed: %s", e)
        return None
    if not isinstance(info, dict) or not info.get("available"):
        return None
    return info


def _is_binary_target(target):
    name = os.path.basename(target).lower()
    if name.endswith(".py") or name.endswith(".pyc"):
        return False
    if "python" in name:
        return False
    return True


def download_binary(url, token, log, insecure=False, max_bytes=MAX_BIN_BYTES):
    """分块下载二进制，带大小上限保护；返回 bytes。"""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    opener = _build_opener(insecure)
    buf = bytearray()
    with opener.open(req, timeout=60) as resp:
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise RuntimeError("binary too large: >%d bytes" % max_bytes)
    return bytes(buf)


def verify_and_replace(data, expected_sha256, target):
    """写临时文件→校验 sha256→原子替换 target；成功返回 True。

    清单缺失 sha256 或校验不匹配均拒绝替换（协议承诺：替换前强制校验，
    防止清单被篡改后写入任意二进制）。失败时清理临时文件，避免残留
    .kk-agent.update.* 占用磁盘。
    """
    digest = hashlib.sha256(data).hexdigest()
    if not expected_sha256:
        raise RuntimeError("manifest missing sha256, refuse to replace")
    if digest.lower() != str(expected_sha256).lower():
        raise RuntimeError("sha256 mismatch: got %s expect %s" % (digest, expected_sha256))
    d = os.path.dirname(os.path.abspath(target))
    tmp = os.path.join(d, ".kk-agent.update.%d" % os.getpid())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o755)
        os.replace(tmp, target)  # 同文件系统原子替换
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return True


def apply_manifest(cfg, log, manifest):
    """按服务端清单下载、校验、替换并自重启。

    manifest: {version, sha256, size, url}；url 可为相对路径（基于管理 API 基址）。
    非二进制形态（源码运行）只下载校验，不 execv。
    """
    log = _log(log)
    ver = manifest.get("version")
    if not ver or not version_lt(kk_config.AGENT_VER, ver):
        return False
    target = cfg.get("agent_bin") or sys.executable
    base = _http_base(cfg.get("server", ""), cfg.get("update_url", ""))
    url = manifest.get("url", "")
    if url and not url.startswith("http"):
        url = "%s%s/download" % (base, UPDATE_PATH) if url == "/download" else (base + url)
    if not url:
        url = "%s%s/download" % (base, UPDATE_PATH)
    log.info("agent update available: %s -> %s, downloading", kk_config.AGENT_VER, ver)
    with _update_lock:  # 串行化下载+替换，避免与轮询更新并发竞争同一二进制
        data = download_binary(url, cfg.get("token", ""), log, cfg.get("update_insecure"))
        verify_and_replace(data, manifest.get("sha256", ""), target)
        log.info("agent binary replaced (%d bytes); restarting", len(data))
        if _is_binary_target(target):
            os.execv(target, [target] + sys.argv[1:])  # 替换当前进程，由新版本接管
        else:
            log.warning("running from source interpreter; update downloaded but not auto-applied, "
                        "restart the agent manually to pick up the new binary")
    return True


def check_update(cfg, log):
    """轮询入口：拉取清单，若有更新则下载应用。设计为在一次性 daemon 线程内调用。"""
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
    """主循环里调用：起一个 daemon 线程做更新检查，不阻塞事件循环。"""
    threading.Thread(target=check_update, args=(cfg, log), daemon=True, name="kk-update").start()


def spawn_apply(cfg, log, manifest):
    """服务端 push 升级帧时调用：立即下载应用。"""
    threading.Thread(target=apply_manifest, args=(cfg, log, manifest), daemon=True,
                     name="kk-update-push").start()
