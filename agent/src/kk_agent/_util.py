"""跨模块共用的小工具（QR1128-A6）。

collector 与 hostinfo 曾各持一份逐字相同的 `_safe`——同类语义两份实现，
历史上「两边各修一次」正是 QR29-A7 收口 `_deltas` 的动机。收敛到这里，
两侧以私有别名导入，调用点与既有测试零改动。
"""


def safe(fn, default=None):
    """单项级容错：单点异常不能拖垮整帧（心跳 / status）。

    返回 None 与抛异常同型处理：都回落 default，调用方因此不用再判 None。
    """
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default