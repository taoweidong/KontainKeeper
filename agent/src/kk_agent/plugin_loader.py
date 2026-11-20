"""自定义采集插件：plugins/ 目录下任意 *.py，实现 collect() -> dict 即可。

- 每次心跳前扫描目录，按 mtime 变化热加载，无需重启 Agent
- 单个插件加载/执行失败只跳过该插件，绝不影响主循环
- 插件输出必须是 JSON 可序列化对象，随心跳 custom 字段上报；不可序列化的输出
  在 collect 出口即按失败隔离（QR-A11）——序列化发生在 transport.publish_hb 的
  json.dumps，放到那里才失败会让整帧心跳（含全部采集项）每个周期一起丢。
  检查口径必须与发布路径完全一致（QR1120-A1）：publish_hb 用
  ensure_ascii=False 后再 encode("utf-8")，孤立代理项（surrogateescape 解码
  产物）与深层嵌套（RecursionError）在默认 ensure_ascii=True 的检查下会漏网。
- 序列化与尺寸检查在**带超时的 worker 内**执行（QR1120-A2）：超大/超深对象图
  可让检查本身任意耗时，放在超时保护外会拖停 busy 期间的后续心跳。
- 单插件输出封顶 MAX_OUTPUT_BYTES（QR1120-A3）：超限按坏输出同款隔离，
  防止单插件把心跳帧撑到任意大。
- collect() 带超时保护（资源评审 P2）：Python 无法强杀线程，插件卡死时
  该插件被**隔离**（quarantine）——直到文件 mtime 变化（作者修改重载）
  才重新放行；否则每个心跳周期都会泄漏一个卡死的执行线程，心跳停摆。
- exec_module 同样带超时（QR-A3）：插件顶层代码卡死（顶层网络调用等）曾会让
  _lock 被永久占住，心跳与 shell 命令线程全体饿死。加载挪到锁外 + 超时隔离，
  卡死的加载线程同样留作 daemon 自生自灭。
"""
import importlib.util
import json
import os
import threading
import traceback

# name -> (mtime, module, quarantined)
_loaded = {}
# 正在加载的名字集合（_lock 守卫）：加载挪到锁外后靠它防同一文件被双重 exec_module
# （module 顶层代码跑两遍，线程/句柄等副作用全翻倍）
_loading = set()
# _loaded/_loading 的并发守卫：心跳线程与 kind=collect 命令线程会同时进 collect_all。
# 锁只包状态读写；exec_module 与 collect() 都在锁外跑——exec_module 可能永久卡死，
# 锁内执行会让锁被永久占住（QR-A3 的根因），collect() 则不该拖慢并发采集。
_lock = threading.Lock()

# 单插件输出的序列化后字节上限（QR1120-A3）：超限按坏输出同款隔离，
# 防止单插件把心跳帧撑到任意大。
MAX_OUTPUT_BYTES = 64 * 1024


def _collect_with_timeout(mod, timeout):
    """在一次性 daemon 线程里跑 collect() + 发布口径的序列化/尺寸检查，
    返回 (data, hung, exc, bad_out)。

    - hung=True：collect 或序列化检查超时卡死（QR1120-A2：超大/超深对象图
      可让检查本身任意耗时，必须与 collect 同在一个超时窗口内）。执行线程
      无法回收，留作 daemon 自生自灭，由调用方隔离该插件。
    - exc：collect 抛异常——偶发故障，仅 warning 不隔离。
    - bad_out：序列化失败或输出超限——坏输出，按失败隔离（QR1120-A1/A3）。

    序列化口径与 transport.publish_hb 完全一致（QR1120-A1）：
    ensure_ascii=False 后 encode("utf-8")。默认 ensure_ascii=True 会把孤立
    代理项转义放行，发布时 utf-8 编码抛 UnicodeEncodeError 丢整帧心跳；
    RecursionError（深层嵌套）也不在旧的 (TypeError, ValueError) 捕获内。
    """
    box = {}

    def work():
        try:
            data = mod.collect()
        except Exception:
            box["exc"] = traceback.format_exc(limit=1)
            return
        try:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception as e:
            box["bad_out"] = "not JSON-serializable: %s" % e
            return
        if len(payload) > MAX_OUTPUT_BYTES:
            box["bad_out"] = ("output too large: %d bytes > %d limit"
                              % (len(payload), MAX_OUTPUT_BYTES))
            return
        box["data"] = data

    t = threading.Thread(target=work, daemon=True, name="kk-plugin")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, True, None, None
    return box.get("data"), False, box.get("exc"), box.get("bad_out")


def _exec_with_timeout(spec, timeout):
    """在一次性 daemon 线程里跑 exec_module，返回 (mod, hung, exc)。

    与 _collect_with_timeout 同一隔离语义：加载卡死的线程无法回收，插件按
    mtime 变化重载为止隔离（module 对象为 None，绝不能放行半初始化的模块）。
    """
    mod = importlib.util.module_from_spec(spec)
    box = {}

    def work():
        try:
            spec.loader.exec_module(mod)
            box["done"] = True
        except Exception:
            box["exc"] = traceback.format_exc(limit=2)

    t = threading.Thread(target=work, daemon=True, name="kk-plugin-load")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, True, None
    return mod, False, box.get("exc")


def collect_all(plugin_dir, log=None, timeout=5.0):
    out = {}
    try:
        entries = sorted(os.listdir(plugin_dir))
    except OSError:
        return out
    for fn in entries:
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        name = fn[:-3]
        path = os.path.join(plugin_dir, fn)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        with _lock:
            ent = _loaded.get(name)
            if ent is not None and ent[0] == mtime:
                cur_mtime, mod, quarantined = ent
                need_load = False
            elif name in _loading:
                continue   # 另一个线程正在加载同一文件：本轮跳过，防双重 exec_module
            else:
                _loading.add(name)
                need_load = True
        if need_load:
            hung, exc, mod = False, None, None
            try:
                spec = importlib.util.spec_from_file_location("kk_plugin_" + name, path)
                if spec is None or spec.loader is None:
                    exc = "no import spec for %s" % path
                else:
                    mod, hung, exc = _exec_with_timeout(spec, timeout)
            finally:
                with _lock:
                    _loading.discard(name)
                    if hung:
                        _loaded[name] = (mtime, None, True)   # 与 collect 卡死同款隔离
                    elif exc:
                        _loaded.pop(name, None)
                    elif mod is not None:
                        _loaded[name] = (mtime, mod, False)   # 重载即解除隔离
            if log and hung:
                log.warning("plugin %s load timed out (%ss), quarantined until reload",
                            name, timeout)
            if log and exc:
                log.warning("plugin %s load failed: %s", name, exc)
            if hung or exc:
                continue
            cur_mtime, mod, quarantined = mtime, mod, False
        if quarantined:
            continue  # 已隔离的卡死插件：不再占用心跳线程
        if not hasattr(mod, "collect"):
            continue
        data, hung, exc, bad_out = _collect_with_timeout(mod, timeout)
        if hung:
            with _lock:
                _loaded[name] = (cur_mtime, mod, True)
            if log:
                log.warning("plugin %s collect timed out (%ss), quarantined until reload",
                            name, timeout)
            continue
        if exc:
            if log:
                log.warning("plugin %s collect failed: %s", name, exc)
            continue
        if bad_out is not None:
            # QR-A11 + QR1120-A1/A3：序列化边界前移到 collect 出口（且在超时
            # worker 内完成）。transport.publish_hb 的 dumps/encode 失败会杀死
            # 整帧心跳（含全部采集项）——坏输出/超限输出按 collect 失败同款
            # 隔离，作者改文件重载后自动放行。
            with _lock:
                _loaded[name] = (cur_mtime, mod, True)
            if log:
                log.warning("plugin %s output rejected (%s), quarantined until reload",
                            name, bad_out)
            continue
        if data is not None:
            out[name] = data
    return out


def loaded_names():
    with _lock:
        return sorted(_loaded.keys())
