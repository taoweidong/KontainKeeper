"""自定义采集插件：plugins/ 目录下任意 *.py，实现 collect() -> dict 即可。

- 每次心跳前扫描目录，按 mtime 变化热加载，无需重启 Agent
- 单个插件加载/执行失败只跳过该插件，绝不影响主循环
- 插件输出必须是 JSON 可序列化对象，随心跳 custom 字段上报
"""
import importlib.util
import os
import traceback

_loaded = {}  # name -> (mtime, module)


def collect_all(plugin_dir, log=None):
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
                _loaded[name] = (mtime, mod)
            except Exception:
                _loaded.pop(name, None)
                if log:
                    log.warning("plugin %s load failed: %s", name, traceback.format_exc(limit=2))
                continue
        mod = _loaded[name][1]
        if not hasattr(mod, "collect"):
            continue
        try:
            data = mod.collect()
            if data is not None:
                out[name] = data
        except Exception:
            if log:
                log.warning("plugin %s collect failed: %s", name, traceback.format_exc(limit=1))
    return out


def loaded_names():
    return sorted(_loaded.keys())
