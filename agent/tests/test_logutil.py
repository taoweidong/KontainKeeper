"""A7：Agent 日志适配层（loguru 后端）单测。

主线：**业务侧 30+ 处 `%s` 懒格式化调用点零改动**，以及 D1（KK_LOG 双写入）的回归锁。
"""
from pathlib import Path

from loguru import logger as _loguru

from kk_agent import logutil


def _capture(name, level="TRACE"):
    """给某个 name 挂一个临时 sink，返回 (records, 清理函数)。"""
    records = []
    hid = _loguru.add(lambda m: records.append(m.record),
                      level=level,
                      filter=lambda r: r["extra"].get("name") == name)
    return records, (lambda: _loguru.remove(hid))


# ---- 适配层：%s 语义 ----

def test_adapter_percent_format_compat():
    """`log.info("a %s b %d", "x", 1)` 经适配器后消息为 `a x b 1`（锁死调用点语义）。"""
    records, done = _capture("t-fmt")
    log = logutil.get_logger(name="t-fmt")
    try:
        log.info("a %s b %d", "x", 1)
        log.warning("mqtt connect failed: %s", "rc5")
    finally:
        done()
    assert records[-2]["message"] == "a x b 1"
    assert records[-1]["message"] == "mqtt connect failed: rc5"


def test_adapter_no_args_untouched():
    """无参数时**不执行** % —— "plain %d" 不得因缺参抛 TypeError。"""
    records, done = _capture("t-noargs")
    log = logutil.get_logger(name="t-noargs")
    try:
        log.warning("plain %d")          # 不抛即通过
        log.warning("disk 50% used")
    finally:
        done()
    assert records[-2]["message"] == "plain %d"
    assert records[-1]["message"] == "disk 50% used"


def test_adapter_bad_placeholder_falls_back():
    """占位符与参数不匹配时退回拼接，绝不因日志丢信息或抛错。"""
    records, done = _capture("t-badfmt")
    log = logutil.get_logger(name="t-badfmt")
    try:
        log.info("has %d %d", 1)         # 少给一个参数
    finally:
        done()
    assert "has %d %d" in records[-1]["message"]


def test_exception_carries_traceback():
    """except 块内 log.exception 必须带上活动异常（58 处调用点不改写的前提）。"""
    records, done = _capture("t-exc")
    log = logutil.get_logger(name="t-exc")
    try:
        try:
            1 / 0
        except ZeroDivisionError:
            log.exception("boom")
    finally:
        done()
    rec = records[-1]
    assert rec["message"] == "boom"
    assert rec["exception"] is not None and rec["exception"].type is ZeroDivisionError


def test_adapter_bind_carries_context():
    """bind 透传：记录带上组件/主机等上下文（D4 的正解）。"""
    records, done = _capture("t-bind")
    log = logutil.get_logger(name="t-bind")
    try:
        log.bind(component="transport", host="web-07").info("hi")
    finally:
        done()
    extra = records[-1]["extra"]
    assert extra["component"] == "transport" and extra["host"] == "web-07"
    assert extra["name"] == "t-bind"


def test_file_sink_renders_bound_context(tmp_path):
    """bind 的上下文在**纯文本** sink 里可见。

    loguru 的 extra 只在 JSON 模式自然可见；不显式渲染 ctx，`bind(host=...)` 在
    docker logs 里就等于没绑。component 由格式单独渲染，不得在 ctx 里重复一遍。
    """
    logfile = tmp_path / "ctx.log"
    log = logutil.get_logger(path=str(logfile), name="t-ctx")
    log.bind(component="transport", host="web-07").info("connected")
    line = logfile.read_text(encoding="utf-8").strip()
    assert "[transport host=web-07] connected" in line
    assert "component=" not in line


# ---- sink 管理 ----

def test_get_logger_idempotent():
    """同一 name 重复调用不得翻倍 sink（否则一条日志打 N 遍）。"""
    logutil.get_logger(name="t-idem")
    n1 = len(logutil._CONFIGURED["t-idem"]["sinks"])
    handlers1 = len(_loguru._core.handlers)
    logutil.get_logger(name="t-idem")
    assert len(logutil._CONFIGURED["t-idem"]["sinks"]) == n1
    assert len(_loguru._core.handlers) == handlers1, "loguru handler 数不得增长"


def test_get_logger_no_file_sink_when_empty(tmp_path):
    """path 为空 / "-" 均不建文件 sink（与旧语义一致）。"""
    logutil.get_logger(path="", name="t-nopath")
    logutil.get_logger(path="-", name="t-dash")
    assert len(logutil._CONFIGURED["t-nopath"]["sinks"]) == 1
    assert len(logutil._CONFIGURED["t-dash"]["sinks"]) == 1


def test_get_logger_file_rotation_and_retention(tmp_path):
    """写入超阈值要出现轮转文件，且总数受 retention 约束（不再靠 wrapper 的裸 mv）。"""
    logfile = tmp_path / "agent.log"
    log = logutil.get_logger(path=str(logfile), name="t-rot")
    line = "x" * 1024
    for _ in range(1500):        # ~1.5MB > rotation="1 MB"
        log.info("%s", line)
    files = sorted(tmp_path.glob("agent*.log*"))
    assert logfile.exists(), "主日志文件必须存在"
    assert len(files) >= 2, "写入超过阈值后必须发生轮转"
    assert len(files) <= 3, "retention=2：主文件 + 至多 2 个轮转副本"


# ---- D1 回归锁 ----

def test_wrapper_never_redirects_agent_output_to_kk_log():
    """静态检查：wrapper 的**代码行**不得把 Agent 输出重定向进 $KK_LOG（D1 回归锁）。

    注释里可以提到该模式（用于说明为什么不能这么做），故只看非注释行。
    """
    sh = Path(__file__).resolve().parent.parent / "deploy" / "entrypoint-wrapper.sh"
    text = sh.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert '>>"$KK_LOG"' not in code
    assert '>"$KK_LOG"' not in code
    assert "rotate_log" not in code, "轮转归 Agent（loguru），wrapper 不应再有 rotate_log"
