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

# 更新失败的指数退避（B6.3）：5min → 10min → 30min 封顶。
# 不加退避时，面对一个坏包（sha256 不符）或只读磁盘，轮询（300s）与每次上线都会
# 重试同一版本 —— 无限重试 + 日志刷屏，500 台一起刷还会把服务端打满。
# 成功或**版本号变化**即清零，所以上传修好的包会立刻得到一次机会，不受退避阻挡。
FAIL_BACKOFF = (300, 600, 1800)
_NO_BACKOFF = frozenset({"not_newer", "backoff"})   # 非失败：无更新 / 本身就在退避中
_fail_state = {"count": 0, "until": 0.0, "ver": ""}


def _in_backoff(now=None):
    now = time.monotonic() if now is None else now
    return now < _fail_state["until"]


def note_update_failure(ver=""):
    """记一次失败并返回本次退避秒数（模块级，供测试断言）。"""
    n = _fail_state["count"] + 1
    wait = FAIL_BACKOFF[min(n, len(FAIL_BACKOFF)) - 1]
    _fail_state.update(count=n, until=time.monotonic() + wait, ver=str(ver or ""))
    return wait


def reset_update_failure():
    _fail_state.update(count=0, until=0.0, ver="")


def _note_result(ver, ok, reason):
    if ok:
        reset_update_failure()
    elif reason not in _NO_BACKOFF:
        note_update_failure(ver)


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

    注意：基址只对**相对** url 才是必需的。服务端下发的清单若带绝对地址
    （KK_PUBLIC_URL 配好时会这样），镜像侧零配置即可完成推送式更新——
    这是 A6.1 修复「推送静默失效」的关键。
    """
    return (cfg.get("update_url") or "").strip().rstrip("/")


def resolve_download_url(cfg, log, manifest):
    """清单里的 url 转成可直接下载的地址；拿不到就返回空串。

    绝对即用、相对才回落：服务端下发的 url 已是绝对地址时不再依赖
    KK_UPDATE_URL（镜像构建脚本不烧入该键，旧实现因此必然静默跳过）。
    """
    log = _log(log)
    url = str(manifest.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    base = _api_base(cfg)
    if not base:
        # 静默失败正源于日志级别过低：这里必须让运维看见
        log.warning("no download address: manifest carries no absolute url and "
                    "KK_UPDATE_URL is unset; configure KK_UPDATE_URL or set "
                    "KK_PUBLIC_URL on the server so it can send absolute urls")
        return ""
    if not url:
        url = "%s/download" % UPDATE_PATH
    return base + (url if url.startswith("/") else "/" + url)


def _build_opener(insecure):
    if not insecure:
        return urllib.request.build_opener()
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def _http_get(url, timeout=15, as_bytes=False, insecure=False):
    req = urllib.request.Request(url)
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
        info = json.loads(_http_get(url, timeout=15, insecure=cfg.get("update_insecure")))
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


def download_binary(url, log, insecure=False, max_bytes=MAX_BIN_BYTES):
    """分块下载二进制，带大小上限保护。"""
    req = urllib.request.Request(url)
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


def apply_manifest(cfg, log, manifest, on_before_restart=None):
    """按服务端清单下载、校验、替换并自重启。

    铁律：形态校验全部通过后才允许下载与落盘。任一步不满足即返回 False，
    且不产生任何副作用。

    on_before_restart：execv 之前的钩子（B6.1 用它发 reason=updating 的状态帧）。
    钩子失败只记日志，不影响更新主流程。
    """
    ok, _ = apply_manifest_receipt(cfg, log, manifest, on_before_restart)
    return ok


def apply_manifest_receipt(cfg, log, manifest, on_before_restart=None):
    """同 apply_manifest，但额外返回失败原因码（A6.2）。

    升级是全平台唯一没有回执的操作——服务端不知道自己推的更新有没有生效，
    500 台里失败多少台无从得知。原因码进台账，让失败可查。

    这里是**轮询与推送两条路径的唯一收口**（check_update 与 kind=update 都经此），
    故失败退避（B6.3）的门禁与记账都放在这一层：同一版本在退避窗口内直接跳过，
    不再下载、不再刷日志；一旦版本号变化或成功即清零。
    """
    ver = str((manifest or {}).get("version") or "")
    if ver and ver == _fail_state["ver"] and _in_backoff():
        _log(log).debug("update to %s still in backoff, skip", ver)
        return False, "backoff"
    ok, reason = _apply_manifest(cfg, log, manifest, on_before_restart)
    _note_result(ver, ok, reason)
    return ok, reason


def _apply_manifest(cfg, log, manifest, on_before_restart=None):
    """执行一次完整的下载/校验/替换/重启；不含退避门禁（由外层负责）。"""
    log = _log(log)
    ver = manifest.get("version")
    if not ver or not version_lt(kk_config.AGENT_VER, ver):
        return False, "not_newer"

    target = cfg.get("agent_bin") or _default_target()
    if not target:
        log.info("agent %s available, but no self-replace target configured "
                 "(source-mode run); set KK_AGENT_BIN to enable self-update", ver)
        return False, "no_target"
    if not _is_binary_target(target):
        log.warning("refuse to self-update: target %r is not a standalone binary", target)
        return False, "bad_target"
    if not os.path.exists(target):
        log.warning("refuse to self-update: target %r does not exist", target)
        return False, "bad_target"

    url = resolve_download_url(cfg, log, manifest)
    if not url:
        return False, "no_url"

    log.info("agent update available: %s -> %s, downloading", kk_config.AGENT_VER, ver)
    with _update_lock:
        try:
            data = download_binary(url, log, cfg.get("update_insecure"))
        except Exception as e:
            log.warning("download failed: %s", e)
            return False, "http_error"
        # 先比 size 再比 sha256（B6.4）：截断/半截响应在 size 上就露馅，
        # 早一步拒绝，失败原因也更直观（sha256 不符看不出「短了多少」）。
        declared = manifest.get("size")
        if declared not in (None, ""):
            try:
                declared = int(declared)
            except (TypeError, ValueError):
                declared = None
        if declared is not None and len(data) != declared:
            log.warning("size mismatch: got %d expect %d, refuse to replace",
                        len(data), declared)
            return False, "size_mismatch"
        expected = str(manifest.get("sha256") or "")
        if expected and hashlib.sha256(data).hexdigest().lower() != expected.lower():
            log.warning("sha256 mismatch, refuse to replace")
            return False, "sha256_mismatch"
        if not _verify_signature(data, manifest, cfg, log):
            # sha256 已在上面单独判过，走到这里只剩签名问题
            return False, "hmac_mismatch"
        try:
            verify_and_replace(data, target)
        except Exception as e:
            log.warning("replace failed: %s", e)
            return False, "disk_error"
        log.info("agent binary replaced (%d bytes); restarting", len(data))
        # 成功回执必须在 execv **之前** 发出：进程被替换后来不及发帧。
        # 此刻 os.replace 已返回，判定成功是准确的。
        if on_before_restart:
            try:
                on_before_restart()
            except Exception as e:
                log.warning("on_before_restart hook failed: %s", e)
        os.execv(target, [target] + sys.argv[1:])
    return True, ""


def check_update(cfg, log, on_before_restart=None):
    """轮询入口：拉清单 → 有更新则应用。设计为在一次性 daemon 线程内调用。

    on_before_restart 会透传到 apply_manifest（B6.1：execv 前宣告 reason=updating）。
    """
    log = _log(log)
    if cfg.get("update_disabled"):
        return False
    info = fetch_latest(cfg, log)
    if not info:
        return False
    try:
        return apply_manifest(cfg, log, info, on_before_restart)
    except Exception:
        log.exception("agent self-update failed")
        return False


def spawn_check(cfg, log, on_before_restart=None):
    threading.Thread(target=check_update, args=(cfg, log, on_before_restart),
                     daemon=True, name="kk-update").start()


def spawn_apply(cfg, log, manifest):
    threading.Thread(target=apply_manifest, args=(cfg, log, manifest), daemon=True,
                     name="kk-update-push").start()
