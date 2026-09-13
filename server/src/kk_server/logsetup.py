"""服务端日志装配（A7）：loguru 统一后端 + stdlib 拦截 + 薄适配层。

三条设计要点：

1. **薄适配层**：各模块继续用 stdlib 的 `%s` 懒格式化（`log.warning("x: %s", y)`），
   适配层内部 `msg % args` 后交给 loguru —— 调用点零改动（与 Agent 侧同构）。
2. **InterceptHandler**：把 stdlib 记录转投 loguru，统一接管 uvicorn / paho / httpx。
   不装它，uvicorn 自带 dictConfig 会让「格式统一」静默失效。
3. **幂等是硬要求**：`create_app` 在整套测试里被调用数百次（function 级 fixture），
   每次都 `logger.add()` 会让 sink 线性增长 → 一条日志打几百遍、测试输出爆炸。

其余约定见 AGENTS.md：全项目只允许 loguru；新代码不得再 `import logging`
（stdlib 仅允许出现在本文件的 InterceptHandler 内）。
"""
import logging
import sys

from loguru import logger as _logger

_CONFIGURED = False
_SINK_IDS = []          # 已注册 sink 的 id：测试据此卸载，运维也可查
_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}

_STDOUT_FMT = ("<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
               "<level>{level: <8}</level> "
               "<cyan>{extra[component]}</cyan>{extra[ctx]} <level>{message}</level>")
_FILE_FMT = ("{time:YYYY-MM-DD HH:mm:ss.SSS} {level: <8} "
             "{extra[component]}{extra[ctx]} {message}")


def _norm_level(level):
    lv = str(level or "").upper()
    return lv if lv in _LEVELS else "INFO"


def _patch(record):
    """给缺 component / ctx 的记录兜底（如被拦截的 uvicorn/paho 记录）。

    没有它，`{extra[component]}` 会 KeyError，配合 catch=True 会让这些记录**静默丢失**。
    ctx 是 bind() 拼出的人类可读上下文（如 " host=web01"），未绑定时为空串。
    """
    extra = record["extra"]
    extra.setdefault("component", record.get("name") or "-")
    extra.setdefault("ctx", "")


# 注意：patcher 是 **Logger 级**配置（`logger.patch()`），不是 `add()` 的 sink 参数——
# 写成 `_logger.add(..., patch=_patch)` 会在运行期抛 TypeError（add() 不认识该参数）。
_PATCHED = _logger.patch(_patch)


class _Adapter:
    """stdlib 风格日志门面（保留 `%s` 懒格式化），底层是 loguru。"""

    def __init__(self, logger, name="kk", ctx=""):
        self._logger = logger
        self._name = name
        self._ctx = ctx

    def bind(self, **kw):
        """透传 loguru 的 `bind`：给记录带上主机/命令号等上下文（D4）。

        同时拼一份人类可读的 `ctx`——loguru 的 extra 只在 JSON 模式可见，
        纯文本 sink 里若不显式渲染，绑了等于没绑。
        """
        # component/name 已由格式单独渲染，不进 ctx（否则日志里出现两遍 component=...）
        parts = "".join(" %s=%s" % (k, v) for k, v in sorted(kw.items())
                        if k not in ("component", "name") and v not in (None, ""))
        ctx = self._ctx + parts
        return _Adapter(self._logger.bind(ctx=ctx, **kw), self._name, ctx)

    @staticmethod
    def _fmt(msg, args):
        if not args:
            return str(msg)          # 无参不执行 %（"50% done" 这类含裸 % 的消息不能炸）
        try:
            return str(msg) % args
        except (TypeError, ValueError):
            return "%s %r" % (msg, args)

    def _opt(self, kw, force_exc=False):
        # depth=1：loguru 记到的函数/行号指向**调用方**而非本适配层（opt 自身不加栈帧）
        # exc_info=True 是 stdlib 语义，必须透传（mqtt_bridge 有两处这么用）
        return self._logger.opt(depth=1, exception=force_exc or bool(kw.get("exc_info")))

    def debug(self, msg, *args, **kw):
        self._opt(kw).debug(self._fmt(msg, args))

    def info(self, msg, *args, **kw):
        self._opt(kw).info(self._fmt(msg, args))

    def success(self, msg, *args, **kw):
        self._opt(kw).success(self._fmt(msg, args))

    def warning(self, msg, *args, **kw):
        self._opt(kw).warning(self._fmt(msg, args))

    def error(self, msg, *args, **kw):
        self._opt(kw).error(self._fmt(msg, args))

    def critical(self, msg, *args, **kw):
        self._opt(kw).critical(self._fmt(msg, args))

    def exception(self, msg, *args, **kw):
        self._opt(kw, force_exc=True).error(self._fmt(msg, args))


class InterceptHandler(logging.Handler):
    """stdlib → loguru 的记录转发（唯一允许 import logging 的地方）。"""

    def emit(self, record):
        try:
            level = _logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # 从 emit 的**调用方**（logging 内部）起向上走，跳过整套 logging 帧，
        # 让 loguru 记到的 name/function/line 指向业务调用方而非 logging 内部。
        # depth 从 2 起算：1 是 emit 自身，2 才是它的调用方（Handler.handle）。
        frame, depth = sys._getframe(1), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame, depth = frame.f_back, depth + 1
        # component 显式取 stdlib 的 logger 名（uvicorn.error / paho.mqtt.client）：
        # 栈回溯得到的 name 会落成 "logging"，日志里分辨不出是谁在说话。
        _PATCHED.opt(depth=depth, exception=record.exc_info).bind(
            component=record.name).log(level, record.getMessage())


def setup_logging(settings):
    """注册 sink（幂等）。在 create_app 内调用，保证测试与生产日志行为一致。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = _norm_level(getattr(settings, "log_level", "INFO"))
    path = (getattr(settings, "log_path", "") or "").strip()
    serialize = bool(getattr(settings, "log_json", False))

    try:
        _logger.remove(0)            # 去掉 loguru 默认 sink，避免再打一份
    except ValueError:
        pass

    common = dict(level=level, backtrace=True, diagnose=False, enqueue=False,
                  catch=True)
    # ① stdout：容器场景由 docker 采集（语义不变）
    _SINK_IDS.append(_logger.add(sys.stdout, format=_STDOUT_FMT,
                                 serialize=serialize, **common))
    # ② 文件：服务端长跑，保留规格高于 Agent（20MB × 10 + gzip）
    if path:
        _SINK_IDS.append(_logger.add(path, format=_FILE_FMT, serialize=serialize,
                                     encoding="utf-8", rotation="20 MB",
                                     retention=10, compression="gz", **common))

    # stdlib 拦截：uvicorn / paho / httpx 的记录统一进 loguru
    intercept = InterceptHandler()
    logging.basicConfig(handlers=[intercept], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [intercept]
        lg.propagate = False         # 已挂拦截器，避免再冒泡到 root 重复一遍

    _CONFIGURED = True


def get_logger(name="kk"):
    """取门面。名称即 component（如 "kk.server" / "kk.bridge"）。"""
    return _Adapter(_PATCHED.bind(component=name, ctx=""), name)
