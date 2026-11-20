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
import shutil
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse

from . import config as kk_config

UPDATE_PATH = "/api/system/agent"
MAX_BIN_BYTES = 64 * 1024 * 1024  # 单文件上限 64MB，防 OOM
MAX_MANIFEST_BYTES = 1024 * 1024  # 清单上限 1MB（QR-A23）：正常清单不到它的零头
_CHUNK = 256 * 1024

# 下载半成品的文件名前缀（QR1120-A9 的清扫只认它，绝不碰别的文件）
DOWNLOAD_TMP_PREFIX = ".kk-agent.download."
# QR1120-A8：单次下载的总 wall-clock 时限。timeout=60 只是**单次 socket 读**的
# 超时，慢速滴答源（每 59s 喂一口）能让下载线程长活；总时限兜底，超限按下载
# 失败处理（http_error），半成品照旧清理。
DOWNLOAD_DEADLINE_SEC = 300
# QR1120-A8：更新锁的最长等待。轮询每 interval 无条件 spawn 线程，旧实现在
# `with _update_lock` 上无限等 → 上一轮卡住时每 300s 积一条线程。拿不到锁即
# 跳过本轮：轮询路径静默跳过，推送路径回良性回执 update_busy（rc=0）。
UPDATE_LOCK_WAIT_SEC = 10
# QR1120-A9：启动清扫的陈旧阈值。在途下载 mtime 新鲜，不会被误删；
# 只有 SIGKILL/断电留下的超过 1h 的残留才清。
STALE_TMP_AGE_SEC = 3600

# 串行化自更新（轮询检查与服务端推送可能并发触发），避免两次下载竞争同一二进制
_update_lock = threading.Lock()

# 更新失败的指数退避（B6.3）：5min → 10min → 30min 封顶。
# 不加退避时，面对一个坏包（sha256 不符）或只读磁盘，轮询（300s）与每次上线都会
# 重试同一版本 —— 无限重试 + 日志刷屏，500 台一起刷还会把服务端打满。
# 成功或**版本号变化**即清零，所以上传修好的包会立刻得到一次机会，不受退避阻挡。
FAIL_BACKOFF = (300, 600, 1800)
# QR1120-A7：良性跳过**不是失败**——推送回执映射 rc=0（服务端落 done），
# 也不进退避账本。not_newer=已是最新（execv 后 QoS1 重投同一条 update 帧的
# 常见结局）、backoff=本身就在失败退避窗口内、update_busy=另一次更新正在进行
# （QR1120-A8 的锁超时跳过）。真实失败（校验/下载/execv/未签名拒绝）仍 rc=1。
BENIGN_SKIP_REASONS = frozenset({"not_newer", "backoff", "update_busy"})
_NO_BACKOFF = BENIGN_SKIP_REASONS   # 非失败：无更新 / 退避中 / 锁 busy
_fail_state = {"count": 0, "until": 0.0, "ver": ""}
# 退避账本的并发守卫：轮询线程（spawn_check）与 kind=update 命令线程都会读写
# _fail_state，无锁时读改交错会让退避计数错乱、提前重试坏版本（评审 P1）。
_fail_lock = threading.RLock()


def note_update_failure(ver=""):
    """记一次失败并返回本次退避秒数（模块级，供测试断言）。"""
    with _fail_lock:
        n = _fail_state["count"] + 1
        wait = FAIL_BACKOFF[min(n, len(FAIL_BACKOFF)) - 1]
        _fail_state.update(count=n, until=time.monotonic() + wait, ver=str(ver or ""))
    return wait


def reset_update_failure():
    with _fail_lock:
        _fail_state.update(count=0, until=0.0, ver="")


# QR1120-A10：轮询拉清单的连续失败计数。旧实现失败只记 debug（默认 INFO 不
# 可见），轮询坏了完全无痕。现在首次失败 warning、持续失败每 N 次 warning 一次，
# 成功即清零。模块级状态，测试前后用 reset_fetch_failure() 清。
_FETCH_WARN_EVERY = 10
_fetch_fail_state = {"count": 0}
_fetch_fail_lock = threading.Lock()


def reset_fetch_failure():
    with _fetch_fail_lock:
        _fetch_fail_state["count"] = 0


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


def _stream_capped(req, timeout, max_bytes, what, insecure=False, deadline_sec=None):
    """流式读取并**硬性封顶**（QR-A23）：`KK_UPDATE_URL` 指向异变的源时，
    超大响应体在 `resp.read()` 里一次性成形会把 Agent 顶出内存。

    清单（KB 级 JSON）与二进制（8~12MB）两个下载路径共用同一骨架（QR29-A7），
    封顶语义只有一份；`what` 只进报错文案，区分是哪个资源超限。

    deadline_sec（QR1120-A8）：给出时按单调钟计总时限，超限 RuntimeError。
    `timeout` 只管单次 socket 读，慢速滴答源靠总时限兜底。
    """
    deadline = None if deadline_sec is None else time.monotonic() + deadline_sec
    buf = bytearray()
    with _build_opener(insecure).open(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            if deadline is not None and time.monotonic() > deadline:
                raise RuntimeError("%s download exceeded %ss deadline" % (what, deadline_sec))
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise RuntimeError("%s too large: >%d bytes" % (what, max_bytes))
    return bytes(buf)


def _http_get(url, timeout=15, as_bytes=False, insecure=False,
              max_bytes=MAX_MANIFEST_BYTES):
    """清单侧下载：流式封顶 + 解码（QR29-A7 收口后只剩形状差异）。"""
    req = urllib.request.Request(url)
    data = _stream_capped(req, timeout, max_bytes, "manifest", insecure=insecure)
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
        # QR1120-A10：首次失败与每 _FETCH_WARN_EVERY 次持续失败各 warning 一次，
        # 其余保持 debug——轮询坏了要看得见，又不能每 300s 刷一条 warning。
        with _fetch_fail_lock:
            _fetch_fail_state["count"] += 1
            n = _fetch_fail_state["count"]
        if n == 1 or n % _FETCH_WARN_EVERY == 0:
            log.warning("fetch latest version failed (%d consecutive): %s", n, e)
        else:
            log.debug("fetch latest version failed: %s", e)
        return None
    reset_fetch_failure()
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


def push_update_allowed(cfg):
    """MQTT 推送路径的签名策略（QR-P0-1）。HTTP 轮询路径不经此检查。

    推送更新等价于远程替换二进制，而 v3 的 Broker 默认匿名开放——任何能往
    cmd 主题发消息的客户端都能触发。因此推送路径默认要求签名：

    - 配了 KK_UPDATE_HMAC_KEY：放行，_verify_signature 会强制验签
      （清单没签或签错都过不去）；
    - 未配 key：没有任何可验签的凭据，默认拒绝；内网可信部署显式置
      KK_UPDATE_ALLOW_UNSIGNED=1 才放行。
    """
    if (cfg.get("update_hmac_key") or "").strip():
        return True
    return bool(cfg.get("update_allow_unsigned"))


def _verify_signature_stream(chunks, manifest, cfg, log):
    """sha256/HMAC 判定链的唯一实现（QR29-A4）：chunks 是二进制内容的可迭代块。

    字节版（推送路径/既有测试）喂 `[data]`，文件版（轮询下载路径）喂分块读——
    判定顺序与拒绝原因共用同一份，两条路径的接受口径才不会各自漂移。
    HMAC 与 sha256 同趟增量计算，不要求内容整体驻留内存。
    """
    log = _log(log)
    expected = str(manifest.get("sha256") or "")
    if not expected:
        log.warning("manifest missing sha256, refuse to replace")
        return False
    key = (cfg.get("update_hmac_key") or "").encode()
    sha = hashlib.sha256()
    mac = hmac.new(key, digestmod=hashlib.sha256) if key else None
    for chunk in chunks:
        sha.update(chunk)
        if mac:
            mac.update(chunk)
    digest = sha.hexdigest()
    if digest.lower() != expected.lower():
        log.warning("sha256 mismatch: got %s expect %s", digest, expected)
        return False

    sig = str(manifest.get("sig") or "")
    if mac:
        if not sig or not hmac.compare_digest(mac.hexdigest(), sig.lower()):
            log.warning("HMAC signature mismatch, refuse to replace")
            return False
    elif cfg.get("update_require_sig"):
        log.warning("KK_UPDATE_REQUIRE_SIG=1 but manifest carries no signature, refuse")
        return False
    return True


def _verify_signature(data, manifest, cfg, log):
    """校验 sha256（防损坏）+ 可选 HMAC 签名（防伪造）。返回是否放行。"""
    return _verify_signature_stream([data], manifest, cfg, log)


def _verify_signature_path(path, manifest, cfg, log):
    """_verify_signature 的文件版：分块读盘，不整包读回内存（QR29-A4）。"""
    with open(path, "rb") as f:
        return _verify_signature_stream(iter(lambda: f.read(_CHUNK), b""),
                                        manifest, cfg, log)


def download_binary(url, log, insecure=False, max_bytes=MAX_BIN_BYTES, to_path=None,
                    deadline_sec=DOWNLOAD_DEADLINE_SEC):
    """分块下载二进制，带大小上限保护（QR29-A4：更新期 RSS 峰值不随包体线性）。

    - to_path 给出：流式写入该文件并**边写边算 sha256**，返回 (size, sha256hex)。
      8~12MB 整包驻留会把 25–35MB 常驻口径顶穿；落盘即 fsync，后续 replace
      不再二次写。下载失败/超限删干净半成品后原样抛出。
    - to_path 为空：保留旧契约返回 bytes（字节语义一字不变）。
    封顶语义不变：任何时刻累计超过 max_bytes 立即 RuntimeError。

    deadline_sec（QR1120-A8）：总 wall-clock 时限（单调钟）。timeout=60 只是
    单次 socket 读的超时，慢速滴答源可让下载线程长活、进而让后续轮询线程在
    更新锁上堆积；总时限超限按下载失败抛出，to_path 形态照旧清理半成品。
    """
    req = urllib.request.Request(url)
    if to_path is None:
        return _stream_capped(req, 60, max_bytes, "binary", insecure=insecure,
                              deadline_sec=deadline_sec)
    sha = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + deadline_sec
    try:
        with _build_opener(insecure).open(req, timeout=60) as resp, \
                open(to_path, "wb") as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("binary download exceeded %ss deadline" % deadline_sec)
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError("binary too large: >%d bytes" % max_bytes)
                sha.update(chunk)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.remove(to_path)
        except OSError:
            pass
        raise
    return total, sha.hexdigest()


_BAK_SUFFIX = ".kkbak"   # 旧二进制备份（execv 失败回滚用，QR-A2）


def promote_downloaded(tmp, target):
    """已下载并校验通过的 tmp 二进制上位：备份旧版 → 原子替换（QR29-A4）。

    原 verify_and_replace 的备份/回滚语义一字不变，只是「把 data 再写一遍 tmp」
    这步被流式下载取代——fsync 与 chmod 在下载侧/此处分别完成。tmp 必须与
    target 同目录（下载侧保证），os.replace 才是同文件系统内的原子替换。
    """
    bak = target + _BAK_SUFFIX
    try:
        if os.name == "posix":
            os.chmod(tmp, 0o755)
        if os.path.exists(target):
            shutil.copy2(target, bak)   # 先留后路，再做不可逆替换
        os.replace(tmp, target)  # 同文件系统内原子替换
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        try:
            os.remove(bak)   # 替换没有发生，备份是冗余残留
        except OSError:
            pass
        raise
    return True


def restore_backup(target):
    """execv 失败后还原旧二进制；返回是否还原成功（失败时磁盘仍是新二进制）。"""
    bak = target + _BAK_SUFFIX
    if not os.path.exists(bak):
        return False
    try:
        os.replace(bak, target)
        return True
    except OSError:
        return False


def sweep_stale_downloads(cfg, log=None):
    """QR1120-A9：启动时清扫二进制同目录的陈旧下载半成品。

    进程被 SIGKILL/断电时，`.kk-agent.download.<pid>` 最大 64MB 的残留会永久
    留在磁盘上（正常失败路径有 finally remove，唯独进程横死没有）。这里只认
    DOWNLOAD_TMP_PREFIX 前缀、只清 mtime 超过 STALE_TMP_AGE_SEC 的普通文件——
    在途下载（别的进程/线程正在写）mtime 新鲜，不会误删。任何失败静默 debug：
    清扫是卫生动作，绝不能挡住 Agent 启动。源码形态无替换目标时是空操作。
    """
    log = _log(log)
    target = (cfg or {}).get("agent_bin") or _default_target()
    if not target:
        return
    directory = os.path.dirname(os.path.abspath(target))
    try:
        names = os.listdir(directory)
    except OSError as e:
        log.debug("stale download sweep skipped: %s", e)
        return
    now = time.time()
    for name in names:
        if not name.startswith(DOWNLOAD_TMP_PREFIX):
            continue
        path = os.path.join(directory, name)
        try:
            if not os.path.isfile(path):
                continue
            if now - os.path.getmtime(path) < STALE_TMP_AGE_SEC:
                continue
            os.remove(path)
            log.info("removed stale download leftover: %s", path)
        except OSError as e:
            log.debug("stale download sweep failed for %s: %s", path, e)


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
    with _fail_lock:
        skip = bool(ver) and ver == _fail_state["ver"] and time.monotonic() < _fail_state["until"]
    if skip:
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
    # QR1120-A8：锁等待改为有限超时。轮询路径每 interval 无条件 spawn 线程，
    # 上一轮下载卡在慢速源时，新线程曾在 `with _update_lock` 上无限排队，
    # 每 300s 积一条。拿不到锁即跳过本轮：轮询静默跳过，推送路径经
    # apply_manifest_receipt 回 update_busy 良性回执（rc=0，服务端落 done）。
    if not _update_lock.acquire(timeout=UPDATE_LOCK_WAIT_SEC):
        log.info("another update is in progress, skip this round (lock busy)")
        return False, "update_busy"
    try:
        # 流式下载直落 target 同目录的 tmp（QR29-A4）：同文件系统 os.replace 才
        # 原子；sha256 与 size 都来自下载那一趟的边写边算，不再整包驻留内存。
        tmp = os.path.join(os.path.dirname(os.path.abspath(target)),
                           DOWNLOAD_TMP_PREFIX + "%d" % os.getpid())
        try:
            try:
                size, sha = download_binary(url, log, cfg.get("update_insecure"),
                                            to_path=tmp)
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
            if declared is not None and size != declared:
                log.warning("size mismatch: got %d expect %d, refuse to replace",
                            size, declared)
                return False, "size_mismatch"
            expected = str(manifest.get("sha256") or "")
            if not expected:
                log.warning("manifest missing sha256, refuse to replace")
                return False, "no_sha256"
            if sha.lower() != expected.lower():
                log.warning("sha256 mismatch, refuse to replace")
                return False, "sha256_mismatch"
            if not _verify_signature_path(tmp, manifest, cfg, log):
                # sha256 已在上面单独判过（含缺失），走到这里只剩签名问题
                return False, "hmac_mismatch"
            try:
                promote_downloaded(tmp, target)
            except Exception as e:
                log.warning("replace failed: %s", e)
                return False, "disk_error"
        finally:
            # 拒绝路径的半成品收口：promote 成功后 tmp 已改名，这里是无害空操作
            try:
                os.remove(tmp)
            except OSError:
                pass
        log.info("agent binary replaced (%d bytes); restarting", size)
        # 成功回执必须在 execv **之前** 发出：进程被替换后来不及发帧。
        # 此刻 os.replace 已返回，判定成功是准确的。
        if on_before_restart:
            try:
                on_before_restart()
            except Exception as e:
                log.warning("on_before_restart hook failed: %s", e)
        try:
            os.execv(target, [target] + sys.argv[1:])
        except OSError:
            # execv 失败（ENOEXEC/架构不符等）：磁盘已是新二进制而进程仍跑旧代码，
            # 不还原的话下次重启就是变砖（QR-A2）。还原后按正常失败回执退出，
            # 退避账本照常记账，避免立刻重试同一个坏包。
            log.exception("execv failed, restoring previous binary")
            if restore_backup(target):
                log.info("previous binary restored after execv failure")
            else:
                log.error("restore failed: %s is the NEW binary; verify it manually "
                          "before restarting this host", target)
            return False, "exec_error"
    finally:
        _update_lock.release()
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
