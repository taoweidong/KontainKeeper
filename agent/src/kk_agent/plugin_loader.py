"""自定义采集插件：plugins/ 目录下任意 *.py，实现 collect() -> dict 即可。

- 每次心跳前扫描目录，按 mtime 变化热加载，无需重启 Agent
- 单个插件加载/执行失败只跳过该插件，绝不影响主循环
- 插件输出必须是 JSON 可序列化对象，随心跳 custom 字段上报
- collect() 带超时保护（资源评审 P2）：Python 无法强杀线程，插件卡死时
  该插件被**隔离**（quarantine）——直到文件 mtime 变化（作者修改重载）
  才重新放行；否则每个心跳周期都会泄漏一个卡死的执行线程，心跳停摆。
"""
import importlib.util
import os
import threading
import traceback

# name -> (mtime, module, quarantined)
_loaded = {}


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
        ent = _loaded.get(name)
        if ent is None or ent[0] != mtime:
            try:
                spec = importlib.util.spec_from_file_location("kk_plugin_" + name, path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _loaded[name] = (mtime, mod, False)  # 重载即解除隔离
            except Exception:
                _loaded.pop(name, None)
                if log:
                    log.warning("plugin %s load failed: %s", name, traceback.format_exc(limit=2))
                continue
        cur_mtime, mod, quarantined = _loaded[name]
        if quarantined:
            continue  # 已隔离的卡死插件：不再占用心跳线程
        if not hasattr(mod, "collect"):
            continue
        data, hung, exc = _collect_with_timeout(mod, timeout)
        if hung:
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
    return sorted(_loaded.keys())
