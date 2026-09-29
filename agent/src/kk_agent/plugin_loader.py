"""自定义采集插件：plugins/ 目录下任意 *.py，实现 collect() -> dict 即可。

- 每次心跳前扫描目录，按 mtime 变化热加载，无需重启 Agent
- 单个插件加载/执行失败只跳过该插件，绝不影响主循环
- 插件输出必须是 JSON 可序列化对象，随心跳 custom 字段上报
- collect() 带超时保护（资源评审 P2）：Python 无法强杀线程，插件卡死时
  该插件被**隔离**（quarantine）——直到文件 mtime 变化（作者修改重载）
  才重新放行；否则每个心跳周期都会泄漏一个卡死的执行线程，心跳停摆。
- exec_module 同样带超时（QR-A3）：插件顶层代码卡死（顶层网络调用等）曾会让
  _lock 被永久占住，心跳与 shell 命令线程全体饿死。加载挪到锁外 + 超时隔离，
  卡死的加载线程同样留作 daemon 自生自灭。
"""
import importlib.util
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


def _collect_with_timeout(mod, timeout):
    """在一次性 daemon 线程里跑 collect()，返回 (data, hung, exc)。

    hung=True 表示超时卡死：执行线程无法回收，留作 daemon 自生自灭，
    由调用方负责隔离该插件，防止逐心跳泄漏线程。
    """
    box = {}

    def work():
        try:
            box["data"] = mod.collect()
        except Exception:
            box["exc"] = traceback.format_exc(limit=1)

    t = threading.Thread(target=work, daemon=True, name="kk-plugin")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, True, None
    return box.get("data"), False, box.get("exc")


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
        data, hung, exc = _collect_with_timeout(mod, timeout)
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
        if data is not None:
            out[name] = data
    return out


def loaded_names():
    with _lock:
        return sorted(_loaded.keys())
