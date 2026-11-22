"""_util.safe 的语义锁（QR1128-A6）：collector 与 hostinfo 共用的单点容错。

收敛前两份逐字相同的实现各自被测试覆盖；收敛后语义只锁这一处，
两侧经私有别名导入（hostinfo._safe / collector._safe 仍是同一函数对象）。
"""
from kk_agent import collector, hostinfo
from kk_agent._util import safe


def test_safe_returns_value():
    assert safe(lambda: 42) == 42
    assert safe(lambda: "x", "d") == "x"


def test_safe_none_falls_back_to_default():
    """返回 None 与抛异常同型：都回落 default，调用方不用再判 None。"""
    assert safe(lambda: None) is None
    assert safe(lambda: None, "d") == "d"


def test_safe_swallows_any_exception():
    def boom():
        raise RuntimeError("no /proc on this platform")

    assert safe(boom) is None
    assert safe(boom, "fallback") == "fallback"


def test_both_modules_share_one_implementation():
    """别名导入必须指向同一函数对象——再出现第二份实现即漂移源。"""
    assert collector._safe is safe
    assert hostinfo._safe is safe