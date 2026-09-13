"""日志：loguru 统一后端 + 薄适配层（A7）。

全项目**唯一** import 日志后端的地方。业务侧 30+ 处调用清一色是 stdlib 的
`%s` 懒格式化（`log.warning("mqtt connect failed: %s", rc)`），而 loguru 用 `{}`。
适配层内部做 `msg % args` 再交给 loguru，于是**业务调用点零改动**、回归面最小。
它同时承担三件事：统一格式、统一轮转策略、把 `bind()` 暴露给上下文（A7/D4）。

公开签名 `get_logger(path, level, name)` 保持不变（main.py 一处调用）。
"""
import os
import sys

from loguru import logger as _logger

# 每个 name 只配置一次：reload / 测试里重复调用不得重复加 sink（否则一条日志打 N 遍）
_CONFIGURED = {}
_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
_DEFAULT_SINK_REMOVED = False

# stderr 由 docker 采集，语义与旧实现一致；不显式 colorize，让 loguru 按 TTY 自动判定
# {extra[ctx]}：bind() 拼出的人类可读上下文（如 " host=web01 cmd=7f3a"），空串时不可见
_STDERR_FMT = ("<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
               "<level>{level: <7}</level> "
               "[<cyan>{extra[component]}</cyan>{extra[ctx]}] <level>{message}</level>")
_FILE_FMT = ("{time:YYYY-MM-DD HH:mm:ss.SSS} {level: <7} "
             "[{extra[component]}{extra[ctx]}] {message}")


def _name_filter(name):
    """每个 logger 只收自己 name 的记录：loguru 是全局单例，靠 filter 做隔离。"""
    return lambda record: record["extra"].get("name") == name


class _Adapter:
    """stdlib 风格日志门面（保留 `%s` 懒格式化），底层是 loguru。"""

    def __init__(self, logger, name="kk-agent", ctx=""):
        self._logger = logger
        self._name = name
        self._ctx = ctx

    def bind(self, **kw):
        """透传 loguru 的 `bind`：给记录带上组件/主机/命令号等上下文（D4）。

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
        # 无参时**不执行** %：否则 "50% done" 这类含裸 % 的消息会抛 ValueError
        if not args:
            return str(msg)
        try:
            return str(msg) % args
        except (TypeError, ValueError):
            # 占位符与参数不匹配：退回原样拼接，绝不因日志丢信息或抛错
            return "%s %r" % (msg, args)

    def _opt(self, kw, force_exc=False):
        # depth=1：loguru 记到的函数/行号指向**调用方**而非本适配层（opt 自身不加栈帧）
        # exc_info=True 是 stdlib 语义，必须透传，否则 except 块里的 traceback 会静默丢失
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
        """在 except 块内调用时附带 traceback。

        适配器方法在调用栈上仍处于调用方的 except 块内，`sys.exc_info()` 可取到
        活动异常，`opt(exception=True)` 因此能正确带上堆栈 —— 58 处 `log.exception`
        无需改写（A7.1 已确认）。
        """
        self._opt(kw, force_exc=True).error(self._fmt(msg, args))


def get_logger(path="", level="INFO", name="kk-agent"):
    """取（或首次建立）一个带 stderr + 可选文件 sink 的日志门面。

    - `path` 为空或 "-" 时不建文件 sink（与旧语义一致）
    - 文件轮转/保留交给 loguru：`rotation="1 MB"`, `retention=2`，**不压缩**
      （Agent 受 nice 降权，不做无收益的 gzip）
    - 幂等：同一 name 重复调用只返回既有门面，不重复加 sink
    """
    lv = str(level or "").upper()
    if lv not in _LEVELS:
        lv = "INFO"        # 未知级别回落，不抛（KK_LOG_LEVEL 大小写/取值失配的兜底）

    bound = _logger.bind(name=name, component=name, ctx="")
    if name in _CONFIGURED:
        return _Adapter(bound, name)

    global _DEFAULT_SINK_REMOVED
    if not _DEFAULT_SINK_REMOVED:
        # loguru 自带 sink(id=0) 会用它的格式再打一份到 stderr —— 去掉，格式由我们定
        try:
            _logger.remove(0)
        except ValueError:
            pass
        _DEFAULT_SINK_REMOVED = True

    # catch=True：日志自身出错（磁盘满/路径不可写）不炸 Agent，等价旧实现的 except OSError
    # diagnose=False 是生产红线：True 会把**局部变量值**（可能含口令）写进日志
    # enqueue=False：不引入 multiprocessing 队列与 feeder 线程，保持现有同步写语义
    common = dict(level=lv, filter=_name_filter(name), catch=True,
                  backtrace=True, diagnose=False, enqueue=False)
    sinks = [_logger.add(sys.stderr, format=_STDERR_FMT, **common)]
    if path and path != "-":
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            sinks.append(_logger.add(path, format=_FILE_FMT, encoding="utf-8",
                                     rotation="1 MB", retention=2,
                                     compression=None, **common))
        except (OSError, ValueError):
            # 文件不可写只降级为 stderr，不让 Agent 起不来（与旧实现一致）
            pass
    _CONFIGURED[name] = {"sinks": sinks, "level": lv}
    return _Adapter(bound, name)
